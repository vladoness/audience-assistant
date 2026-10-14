"""Synthetic telco profile store (one row per mobile line).

Generated entirely in DuckDB SQL with hash-based pseudo-randomness, so the same N always
produces the same rows (for a given DuckDB version) and millions of rows take seconds.
The distributions are invented but carry the traps a real operator's data has: silent lines
whose billing status is still 'current', a top-up attribute that only exists for prepaid,
and consent coverage that differs by channel.

It also generates a year of past campaigns with holdout groups and responses, from known true
effects (CAMPAIGN_TYPES), so the app can show what happened last time and the tests can check
that its measurement recovers the truth. All of it is labelled synthetic wherever it is shown.

Run directly:  uv run python -m app.synthdata --rows 200000 --out data/profiles.duckdb
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

_GENERATE_SQL = """
CREATE OR REPLACE MACRO u(i, s) AS (hash(i, s) % 1000000) / 1000000.0;

CREATE OR REPLACE TABLE profiles AS
WITH base AS (
    SELECT
        i,
        printf('L%08d', i) AS profile_id,
        CASE WHEN u(i, 'lt') < 0.27 THEN 'postpaid' ELSE 'prepaid' END AS line_type,
        CASE
            WHEN u(i, 'rg') < 0.34 THEN 'Capital'
            WHEN u(i, 'rg') < 0.58 THEN 'Coast'
            WHEN u(i, 'rg') < 0.76 THEN 'Highlands'
            WHEN u(i, 'rg') < 0.90 THEN 'Valley'
            ELSE 'North'
        END AS region,
        CASE
            WHEN u(i, 'os') < 0.62 THEN 'android'
            WHEN u(i, 'os') < 0.80 THEN 'ios'
            ELSE 'feature_phone'
        END AS device_os,
        CAST(floor(pow(u(i, 'tn'), 1.6) * 180) AS INTEGER) AS tenure_months
    FROM range(?) t(i)
),
tiered AS (
    SELECT *,
        CASE
            WHEN line_type = 'postpaid' THEN
                CASE WHEN u(i, 'pt') < 0.25 THEN 'premium' WHEN u(i, 'pt') < 0.75 THEN 'standard' ELSE 'basic' END
            ELSE
                CASE WHEN u(i, 'pt') < 0.05 THEN 'premium' WHEN u(i, 'pt') < 0.30 THEN 'standard' ELSE 'basic' END
        END AS plan_tier,
        CASE
            WHEN line_type = 'postpaid' THEN
                CASE WHEN u(i, 'bs') < 0.85 THEN 'current' WHEN u(i, 'bs') < 0.95 THEN 'overdue' ELSE 'suspended' END
            ELSE
                CASE WHEN u(i, 'bs') < 0.92 THEN 'current' ELSE 'suspended' END
        END AS billing_status
    FROM base
),
behaviour AS (
    SELECT *,
        billing_status = 'suspended' OR u(i, 'silent') < 0.09 AS is_silent,
        CASE
            WHEN line_type = 'postpaid' THEN
                CASE plan_tier WHEN 'premium' THEN 45 + u(i, 'ar') * 65
                               WHEN 'standard' THEN 25 + u(i, 'ar') * 25
                               ELSE 15 + u(i, 'ar') * 15 END
            ELSE
                CASE plan_tier WHEN 'premium' THEN 15 + u(i, 'ar') * 30
                               WHEN 'standard' THEN 8 + u(i, 'ar') * 22
                               ELSE 2 + u(i, 'ar') * 23 END
        END AS arpu_raw
    FROM tiered
)
SELECT
    profile_id,
    line_type,
    region,
    plan_tier,
    device_os,
    tenure_months,
    round(CASE WHEN billing_status = 'suspended' THEN arpu_raw * 0.3 ELSE arpu_raw END, 2) AS arpu_3m,
    round(CASE
        WHEN is_silent OR device_os = 'feature_phone' THEN 0
        ELSE pow(u(i, 'dt'), 1.8) * 40 * (CASE plan_tier WHEN 'premium' THEN 1.6 WHEN 'standard' THEN 1.1 ELSE 0.7 END)
    END, 1) AS data_gb_30d,
    CAST(CASE
        WHEN is_silent THEN 0
        WHEN device_os = 'feature_phone' THEN 20 + u(i, 'ev') * 280
        ELSE 50 + u(i, 'ev') * 2450
    END AS INTEGER) AS usage_events_30d,
    (NOT is_silent AND device_os <> 'feature_phone'
        AND u(i, 'app') < 0.15 + (CASE WHEN line_type = 'postpaid' THEN 0.35 ELSE 0 END)
                            + (CASE WHEN arpu_raw > 30 THEN 0.20 ELSE 0 END)) AS app_active_30d,
    (NOT is_silent AND u(i, 'rm') < (CASE WHEN line_type = 'postpaid' THEN 0.07 ELSE 0.02 END)) AS roaming_30d,
    billing_status,
    CASE
        WHEN line_type = 'postpaid' THEN NULL
        WHEN billing_status = 'suspended' THEN CAST(91 + u(i, 'rc') * 274 AS INTEGER)
        ELSE CAST(floor(pow(u(i, 'rc'), 2.2) * 90) AS INTEGER)
    END AS days_since_last_recharge,
    CASE
        WHEN billing_status <> 'current' AND u(i, 'cr') < 0.6 THEN 'high'
        WHEN u(i, 'cr') < 0.15 THEN 'high'
        WHEN u(i, 'cr') < 0.45 THEN 'medium'
        ELSE 'low'
    END AS credit_risk_band,
    u(i, 'cs') < 0.72 AS marketing_consent_sms,
    u(i, 'ce') < (CASE WHEN line_type = 'postpaid' THEN 0.55 ELSE 0.25 END) AS marketing_consent_email
FROM behaviour;
"""


# Synthetic campaign history: the KNOWN TRUTH it is generated from. Each type has an eligible
# audience, a channel, the response rate of lines that were NOT contacted (base) and the true
# effect of contacting them (lift, in absolute response rate). Measurement must recover the lift
# from the data alone; tests/test_history.py checks that it does.
CAMPAIGN_TYPES: dict[str, dict] = {
    "topup_reminder": {"label": "Top-up reminder", "channel": "sms", "base": 0.100, "lift": 0.020,
                       "audience": "line_type = 'prepaid' AND days_since_last_recharge BETWEEN 15 AND 45"},
    # One planted segment effect: upsells work clearly better on postpaid lines. The advisor should
    # find this and nothing else (every other type has the same lift in every segment).
    "data_upsell": {"label": "Data bundle upsell", "channel": "sms", "base": 0.020, "lift": 0.008,
                    "lift_by": {"attribute": "line_type", "values": {"postpaid": 0.016, "prepaid": 0.005}},
                    "audience": "data_gb_30d >= 5"},
    "roaming_offer": {"label": "Roaming offer", "channel": "email", "base": 0.030, "lift": 0.012,
                      "audience": "roaming_30d OR (line_type = 'postpaid' AND plan_tier = 'premium')"},
    "winback": {"label": "Lapsed prepaid win-back", "channel": "sms", "base": 0.050, "lift": 0.010,
                "audience": "line_type = 'prepaid' AND days_since_last_recharge > 30"},
    "app_adoption": {"label": "App adoption", "channel": "sms", "base": 0.040, "lift": 0.003,
                     "audience": "NOT app_active_30d AND device_os <> 'feature_phone'"},
}
HISTORY_VERSION = 2  # bump when CAMPAIGN_TYPES change, so existing demo stores regenerate their history
HISTORY_CAMPAIGNS = 60  # one every 6 days over the last year, types in rotation
HISTORY_SAMPLE_PCT = 40  # share of the eligible, consented lines each past campaign reached
HISTORY_HOLDOUT_PCT = 10


def generate_history(con: duckdb.DuckDBPyConnection, as_of: date) -> None:
    """Create (or replace) `campaigns`, `campaign_contacts` and `history_meta` from CAMPAIGN_TYPES."""
    types = list(CAMPAIGN_TYPES)
    con.execute("CREATE OR REPLACE TABLE history_meta AS SELECT ?::DATE AS as_of, true AS synthetic, ? AS version",
                [as_of, HISTORY_VERSION])
    con.execute("CREATE OR REPLACE TABLE campaigns (campaign_id INTEGER, campaign_type VARCHAR, name VARCHAR, "
                "channel VARCHAR, sent_at DATE)")
    for k in range(HISTORY_CAMPAIGNS):
        t = types[k % len(types)]
        sent = as_of - timedelta(days=2 + 6 * (HISTORY_CAMPAIGNS - 1 - k))
        con.execute("INSERT INTO campaigns VALUES (?, ?, ?, ?, ?)",
                    [k + 1, t, f"{CAMPAIGN_TYPES[t]['label']}, {sent:%d %b %Y}", CAMPAIGN_TYPES[t]["channel"], sent])
    eligible = " OR ".join(
        f"(c.campaign_type = '{t}' AND ({spec['audience']}) AND "
        f"{'marketing_consent_sms' if spec['channel'] == 'sms' else 'marketing_consent_email'})"
        for t, spec in CAMPAIGN_TYPES.items())
    rate = " ".join(f"WHEN '{t}' THEN {spec['base']} + CASE WHEN held_out THEN 0 ELSE {lift_sql(spec)} END"
                    for t, spec in CAMPAIGN_TYPES.items())
    con.execute(f"""
        CREATE OR REPLACE TABLE campaign_contacts AS
        WITH reached AS (
            SELECT c.campaign_id, c.campaign_type, p.profile_id, p.line_type, p.region, p.plan_tier, p.device_os,
                   hash(p.profile_id, c.campaign_id, 'h') % 100 < {HISTORY_HOLDOUT_PCT} AS held_out
            FROM campaigns c JOIN profiles p ON ({eligible})
            WHERE hash(p.profile_id, c.campaign_id, 's') % 100 < {HISTORY_SAMPLE_PCT}
        )
        SELECT campaign_id, profile_id, held_out,
               (hash(profile_id, campaign_id, 'r') % 1000000) / 1000000.0 < (CASE campaign_type {rate} END) AS responded
        FROM reached""")


def lift_sql(spec: dict) -> str:
    """The true lift of a campaign type as SQL over the line's columns (constant, or by segment)."""
    by = spec.get("lift_by")
    if not by:
        return str(spec["lift"])
    cases = " ".join(f"WHEN '{value}' THEN {lift}" for value, lift in by["values"].items())
    return f"(CASE {by['attribute']} {cases} ELSE {spec['lift']} END)"


def true_lift(spec: dict, segment: dict[str, str] | None = None) -> float:
    """The planted lift for a line with these segment values (for tests and the docs)."""
    by = spec.get("lift_by")
    if by and segment and by["attribute"] in segment:
        return by["values"].get(segment[by["attribute"]], spec["lift"])
    return spec["lift"]


def generate(path: Path, rows: int, history: bool = True) -> float:
    """Create (or replace) the profiles table at `path`, and by default the synthetic campaign
    history that goes with it. Returns seconds taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with duckdb.connect(str(path)) as con:
        for statement in _GENERATE_SQL.split(";\n\nCREATE"):
            sql = statement if statement.lstrip().startswith("CREATE") else "CREATE" + statement
            if "range(?)" in sql:
                con.execute(sql, [rows])
            else:
                con.execute(sql)
        con.execute("DROP MACRO IF EXISTS u")
        if history:
            generate_history(con, date.today())
    elapsed = time.perf_counter() - started
    log.info("synthetic_data_generated", extra={"rows": rows, "path": str(path), "seconds": round(elapsed, 2),
                                                "history": history})
    return elapsed


def add_history(path: Path) -> None:
    """Add the synthetic campaign history to an existing synthetic profile store."""
    with duckdb.connect(str(path)) as con:
        generate_history(con, date.today())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=200_000)
    parser.add_argument("--out", type=Path, default=Path("data/profiles.duckdb"))
    args = parser.parse_args()
    seconds = generate(args.out, args.rows)
    print(f"Generated {args.rows:,} profiles in {seconds:.1f}s -> {args.out}")


if __name__ == "__main__":
    main()
