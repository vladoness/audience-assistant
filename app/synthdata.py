"""Synthetic telco profile store (one row per mobile line).

Generated entirely in DuckDB SQL with hash-based pseudo-randomness, so the same N always
produces the same rows (for a given DuckDB version) and millions of rows take seconds.
The distributions are invented but carry the traps a real operator's data has: silent lines
whose billing status is still 'current', a top-up attribute that only exists for prepaid,
and consent coverage that differs by channel.

Run directly:  uv run python -m app.synthdata --rows 200000 --out data/profiles.duckdb
"""

from __future__ import annotations

import argparse
import logging
import time
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


def generate(path: Path, rows: int) -> float:
    """Create (or replace) the profiles table at `path`. Returns seconds taken."""
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
    elapsed = time.perf_counter() - started
    log.info("synthetic_data_generated", extra={"rows": rows, "path": str(path), "seconds": round(elapsed, 2)})
    return elapsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=200_000)
    parser.add_argument("--out", type=Path, default=Path("data/profiles.duckdb"))
    args = parser.parse_args()
    seconds = generate(args.out, args.rows)
    print(f"Generated {args.rows:,} profiles in {seconds:.1f}s -> {args.out}")


if __name__ == "__main__":
    main()
