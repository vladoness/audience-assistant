"""Past campaigns as evidence for the next decision.

Two questions a marketer should be able to answer before approving an audience:
- **Contact pressure:** how many of these lines got a campaign recently?
- **What happened last time:** for past campaigns of the same type, how much better did the
  contacted lines respond than the held-out lines (measured lift, with a 95% interval)?

The answers are evidence, never a recommendation or a forecast. In this prototype the history is
synthetic (generated from known effects in app/synthdata.py) and every result says so; in a real
deployment it comes from the campaign tool's send and response records.

The loop closes here too: when an approved audience's outcome is reported (POST
/api/audiences/{id}/outcome, or a labelled simulation in the demo) and a person confirms it, its
measured lift joins the evidence for its campaign type, so the next decision of that type sees it,
and the approval's own expectation is compared with what happened.

Ontology contract (target, for an AEP rollout): send and response events from Journey Optimizer
datasets, lift from Customer Journey Analytics or Query Service over the same events.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.audience import AudienceEngine
from app.catalog import Catalog
from app.models import AudienceDefinition, ContactPressure, Issue
from app.synthdata import CAMPAIGN_TYPES, lift_sql

Z95 = 1.96
Z_SEGMENT = 2.81  # 99.5%: allows for comparing ~10 segments at once without inventing differences
SEGMENT_ATTRIBUTES = ("line_type", "region", "plan_tier", "device_os")


@dataclass
class Lift:
    contacted: int
    held_out: int
    contacted_rate: float
    held_out_rate: float

    @property
    def points(self) -> float:
        return 100 * (self.contacted_rate - self.held_out_rate)

    def _adjusted(self) -> tuple[float, float, int, int]:
        # Agresti-Caffo: one extra responder and non-responder per group. Without it, a group with no
        # responders has zero variance and a small campaign gets a falsely exact (zero-width) interval.
        n_c, n_h = self.contacted + 2, self.held_out + 2
        return ((self.contacted_rate * self.contacted + 1) / n_c, (self.held_out_rate * self.held_out + 1) / n_h, n_c, n_h)

    @property
    def se_points(self) -> float:
        if not self.contacted or not self.held_out:
            return math.nan
        p_c, p_h, n_c, n_h = self._adjusted()
        return 100 * math.sqrt(p_c * (1 - p_c) / n_c + p_h * (1 - p_h) / n_h)

    @property
    def interval(self) -> tuple[float, float]:
        if not self.contacted or not self.held_out:
            return (math.nan, math.nan)
        p_c, p_h, _, _ = self._adjusted()
        centre = 100 * (p_c - p_h)
        return (centre - Z95 * self.se_points, centre + Z95 * self.se_points)

    def as_dict(self) -> dict[str, Any]:
        low, high = self.interval
        return {"contacted": self.contacted, "held_out": self.held_out,
                "contacted_response_pct": round(100 * self.contacted_rate, 2),
                "held_out_response_pct": round(100 * self.held_out_rate, 2),
                "lift_points": round(self.points, 2), "lift_95_low": round(low, 2), "lift_95_high": round(high, 2)}


class CampaignHistory:
    def __init__(self, engine: AudienceEngine, catalog: Catalog):
        self._engine = engine
        self._catalog = catalog
        con = engine.connection
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        self.available = {"campaigns", "campaign_contacts", "history_meta"} <= tables
        self.as_of, self.synthetic = (con.execute("SELECT as_of, synthetic FROM history_meta").fetchone()
                                      if self.available else (None, False))

    def types(self) -> list[dict[str, Any]]:
        if not self.available:
            return []
        counts = dict(self._engine.connection.execute(
            "SELECT campaign_type, count(*) FROM campaigns GROUP BY 1").fetchall())
        return [{"id": t, "label": spec["label"], "channel": spec["channel"], "response": spec["response"],
                 "campaigns": counts.get(t, 0)}
                for t, spec in CAMPAIGN_TYPES.items()]

    def contact_pressure(self, definition: AudienceDefinition) -> ContactPressure | None:
        """Reachable lines of this audience by how recently and how often past campaigns contacted them."""
        if not self.available:
            return None
        policy = self._catalog.parameter_sets.get("contact_policy")
        recent = int(policy.values["recent_days"]) if policy else 7
        window = int(policy.values["window_days"]) if policy else 30
        heavy = int(policy.values["max_contacts_in_window"]) if policy else 3
        where, params = self._engine.compile(definition)
        consent = f'"{self._catalog.channels[definition.channel]}"'
        reachable, in_recent, in_window, at_cap = self._engine.connection.cursor().execute(f"""
            WITH audience AS (SELECT profile_id FROM profiles WHERE {where} AND {consent}),
            contacts AS (
                SELECT x.profile_id,
                       count(*) FILTER (WHERE k.sent_at > h.as_of - ?::INTEGER) AS n_recent,
                       count(*) FILTER (WHERE k.sent_at > h.as_of - ?::INTEGER) AS n_window
                FROM campaign_contacts x JOIN campaigns k USING (campaign_id), history_meta h
                WHERE NOT x.held_out GROUP BY 1)
            SELECT count(*), count(*) FILTER (WHERE n_recent > 0), count(*) FILTER (WHERE n_window > 0),
                   count(*) FILTER (WHERE n_window >= ?)
            FROM audience LEFT JOIN contacts USING (profile_id)""", [*params, recent, window, heavy]).fetchone()
        warnings = []
        share_recent = in_recent / reachable if reachable else 0.0
        if policy and share_recent > float(policy.values["warn_share_recent"]):
            warnings.append(Issue(level="warning", code="recently_contacted",
                                  message=f"{share_recent:.0%} of these lines got a campaign in the last {recent} days."))
        if at_cap:
            warnings.append(Issue(level="warning", code="contact_cap",
                                  message=f"{at_cap:,} lines already had {heavy} or more campaigns in the last {window} days."))
        return ContactPressure(
            synthetic=bool(self.synthetic), as_of=str(self.as_of), reachable=reachable,
            recent_days=recent, contacted_recently=in_recent, window_days=window, contacted_in_window=in_window,
            cap=heavy, at_or_over_cap=at_cap, warnings=warnings,
            policy=f"contact_policy v{policy.version}" if policy else None,
        )

    def evidence(self, campaign_type: str, own: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """Measured lift of past campaigns of one type: each campaign, and all of them pooled. `own` are
        outcomes measured for audiences approved in this app; they join the evidence, so every measured
        campaign informs the next decision."""
        campaigns = self._campaigns(campaign_type, own)
        if campaigns is None:
            return None
        pooled = pool(campaigns)
        per_campaign = [{k: v for k, v in c.items() if k != "lift"} | c["lift"].as_dict()
                        for c in sorted(campaigns, key=lambda c: c["sent_at"], reverse=True)]
        clearly_positive = sum(1 for c in per_campaign if c["lift_95_low"] > 0)
        measured_here = sum(1 for c in campaigns if c["source"] != SYNTHETIC_SOURCE)
        label = CAMPAIGN_TYPES[campaign_type]["label"]
        return {
            "campaign_type": campaign_type, "label": label,
            "synthetic": bool(self.synthetic), "as_of": str(self.as_of), "campaigns": len(campaigns),
            "measured_here": measured_here,
            "pooled": pooled.as_dict(),
            "clearly_positive_campaigns": clearly_positive,
            "per_campaign": per_campaign,
            "reading": (f"Across {len(campaigns)} past {label.lower()} campaigns"
                        f"{f' ({measured_here} of them approved here)' if measured_here else ''}, contacted "
                        f"lines responded {pooled.points:+.2f} points versus their holdout (95% interval "
                        f"{pooled.interval[0]:+.2f} to {pooled.interval[1]:+.2f}); {clearly_positive} of {len(campaigns)} "
                        f"{'was' if clearly_positive == 1 else 'were'} clearly positive on their own. "
                        "Evidence from past campaigns, not a forecast."),
        }

    def learning(self, campaign_type: str, own: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """The learning curve for one campaign type: every campaign in send order, with the pooled estimate
        and its 95% interval as they stood after each one. The interval narrowing is the learning."""
        campaigns = self._campaigns(campaign_type, own)
        if campaigns is None:
            return None
        timeline, running = [], []
        for c in sorted(campaigns, key=lambda c: (c["sent_at"], c["source"] != SYNTHETIC_SOURCE)):
            running.append(c)
            estimate = pool(running)
            low, high = estimate.interval
            timeline.append({k: v for k, v in c.items() if k != "lift"} | c["lift"].as_dict() | {
                "estimate_after": round(estimate.points, 2), "estimate_low": round(low, 2), "estimate_high": round(high, 2)})
        return {"campaign_type": campaign_type, "label": CAMPAIGN_TYPES[campaign_type]["label"],
                "synthetic": bool(self.synthetic), "timeline": timeline}

    def _campaigns(self, campaign_type: str, own: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not self.available or campaign_type not in CAMPAIGN_TYPES:
            return None
        rows = self._engine.connection.cursor().execute("""
            SELECT k.campaign_id, k.name, k.sent_at,
                   count(*) FILTER (WHERE NOT x.held_out), count(*) FILTER (WHERE x.held_out),
                   count(*) FILTER (WHERE NOT x.held_out AND x.responded), count(*) FILTER (WHERE x.held_out AND x.responded)
            FROM campaigns k JOIN campaign_contacts x USING (campaign_id)
            WHERE k.campaign_type = ? GROUP BY 1, 2, 3""", [campaign_type]).fetchall()
        campaigns = [{"campaign_id": r[0], "name": r[1], "sent_at": str(r[2]), "source": SYNTHETIC_SOURCE,
                      "audience_id": None, "lift": counts_lift(r[3], r[4], r[5], r[6])} for r in rows]
        for o in own or []:
            if o["campaign_type"] == campaign_type:
                campaigns.append({"campaign_id": None, "name": o["name"], "sent_at": o["sent_at"],
                                  "source": f"approved here ({'simulated send' if o['source'] == 'simulated' else 'campaign tool'})",
                                  "audience_id": o["audience_id"],
                                  "lift": counts_lift(o["contacted"], o["held_out"], o["contacted_responders"],
                                                      o["held_out_responders"])})
        return campaigns

    def simulate_responders(self, members: list[tuple[str, str]], campaign_type: str, seed: str) -> list[str]:
        """DEMO ONLY: who would have responded, drawn from the same known true effects that generated the
        synthetic history (held-out lines respond at the base rate, contacted lines at base + lift)."""
        spec = CAMPAIGN_TYPES[campaign_type]
        rows = self._engine.connection.cursor().execute(
            "SELECT m.profile_id FROM (SELECT unnest(string_split(?, chr(31))) AS profile_id, "
            "unnest(string_split(?, chr(31))) = 'held_out' AS held_out) m JOIN profiles USING (profile_id) "
            "WHERE (md5_number_lower(? || ':' || m.profile_id) % 1000000) / 1000000.0 < "
            f"CASE WHEN m.held_out THEN ? ELSE ? + {lift_sql(spec)} END",
            ["\x1f".join(p for p, _ in members), "\x1f".join(g for _, g in members), seed, spec["base"], spec["base"]],
        ).fetchall()
        return [r[0] for r in rows]

    def segment_lift(self, campaign_type: str, attribute: str) -> list[dict[str, Any]]:
        """Lift of past campaigns of one type, per value of one profile attribute, each compared with all
        the other values. A segment is only called clearly different at Z_SEGMENT (99.5%), because the
        advisor looks at many segments at once and some would differ at 95% by chance alone."""
        if not self.available or campaign_type not in CAMPAIGN_TYPES or attribute not in SEGMENT_ATTRIBUTES:
            return []
        rows = self._engine.connection.cursor().execute(f"""
            SELECT p.{attribute},
                   count(*) FILTER (WHERE NOT x.held_out), count(*) FILTER (WHERE x.held_out),
                   count(*) FILTER (WHERE NOT x.held_out AND x.responded), count(*) FILTER (WHERE x.held_out AND x.responded)
            FROM campaign_contacts x JOIN campaigns k USING (campaign_id) JOIN profiles p USING (profile_id)
            WHERE k.campaign_type = ? GROUP BY 1 ORDER BY 1""", [campaign_type]).fetchall()
        total = [sum(r[i] for r in rows) for i in range(1, 5)]
        out = []
        for value, *counts in rows:
            seg = counts_lift(*counts)
            rest = counts_lift(*(t - c for t, c in zip(total, counts)))
            if not (seg.contacted and seg.held_out and rest.contacted and rest.held_out):
                continue  # nothing to compare with (e.g. every past send went to this one value)
            diff = seg.points - rest.points
            se = math.sqrt(seg.se_points ** 2 + rest.se_points ** 2)
            out.append({"attribute": attribute, "value": value, **seg.as_dict(),
                        "rest_lift_points": round(rest.points, 2),
                        "difference_points": round(diff, 2),
                        "clearly_different": bool(se and abs(diff) > Z_SEGMENT * se)})
        return out


SYNTHETIC_SOURCE = "synthetic history"


def counts_lift(contacted: int, held_out: int, contacted_responders: int, held_out_responders: int) -> Lift:
    return Lift(contacted, held_out, contacted_responders / contacted if contacted else 0.0,
                held_out_responders / held_out if held_out else 0.0)


def pool(campaigns: list[dict[str, Any]]) -> Lift:
    """All the contacted lines against all the held-out lines of these campaigns."""
    n_c = sum(c["lift"].contacted for c in campaigns)
    n_h = sum(c["lift"].held_out for c in campaigns)
    r_c = sum(round(c["lift"].contacted_rate * c["lift"].contacted) for c in campaigns)
    r_h = sum(round(c["lift"].held_out_rate * c["lift"].held_out) for c in campaigns)
    return counts_lift(n_c, n_h, r_c, r_h)


def verdict(expected: dict[str, Any] | None, measured: Lift, min_holdout_lines: int = 0) -> str:
    """How a measured campaign compares with what the evidence said when it was approved. One campaign
    is much noisier than the pooled history, so a difference is only claimed when the two 95% intervals
    don't overlap. "In line" is only said of a result that also can't be mistaken for no effect: a small
    holdout, or an interval that includes zero, is "too small to tell", never reassurance."""
    if not expected:
        return "no expectation was recorded at approval"
    if measured.held_out < min_holdout_lines:
        return f"too small to tell (fewer than {min_holdout_lines:,} held-out lines)"
    low, high = (round(v, 2) for v in measured.interval)  # judged on the numbers the reader sees
    if high < expected["lift_95_low"]:
        return "below what past campaigns predicted"
    if low > expected["lift_95_high"]:
        return "above what past campaigns predicted"
    if low <= 0 <= high:
        return "too small to tell (the interval includes no effect)"
    return "in line with past campaigns"
