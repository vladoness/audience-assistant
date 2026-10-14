"""Past campaigns as evidence for the next decision.

Two questions a marketer should be able to answer before approving an audience:
- **Contact pressure:** how many of these lines got a campaign recently?
- **What happened last time:** for past campaigns of the same type, how much better did the
  contacted lines respond than the held-out lines (measured lift, with a 95% interval)?

The answers are evidence, never a recommendation or a forecast. In this prototype the history is
synthetic (generated from known effects in app/synthdata.py) and every result says so; in a real
deployment it comes from the campaign tool's send and response records.

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
from app.synthdata import CAMPAIGN_TYPES

Z95 = 1.96


@dataclass
class Lift:
    contacted: int
    held_out: int
    contacted_rate: float
    held_out_rate: float

    @property
    def points(self) -> float:
        return 100 * (self.contacted_rate - self.held_out_rate)

    @property
    def interval(self) -> tuple[float, float]:
        if not self.contacted or not self.held_out:
            return (math.nan, math.nan)
        se = math.sqrt(self.contacted_rate * (1 - self.contacted_rate) / self.contacted
                       + self.held_out_rate * (1 - self.held_out_rate) / self.held_out)
        return (self.points - 100 * Z95 * se, self.points + 100 * Z95 * se)

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
        return [{"id": t, "label": spec["label"], "channel": spec["channel"], "campaigns": counts.get(t, 0)}
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

    def evidence(self, campaign_type: str) -> dict[str, Any] | None:
        """Measured lift of past campaigns of one type: each campaign, and all of them pooled."""
        if not self.available or campaign_type not in CAMPAIGN_TYPES:
            return None
        rows = self._engine.connection.cursor().execute("""
            SELECT k.campaign_id, k.name, k.sent_at,
                   count(*) FILTER (WHERE NOT x.held_out), count(*) FILTER (WHERE x.held_out),
                   count(*) FILTER (WHERE NOT x.held_out AND x.responded), count(*) FILTER (WHERE x.held_out AND x.responded)
            FROM campaigns k JOIN campaign_contacts x USING (campaign_id)
            WHERE k.campaign_type = ? GROUP BY 1, 2, 3 ORDER BY k.sent_at DESC""", [campaign_type]).fetchall()

        def lift(n_c: int, n_h: int, r_c: int, r_h: int) -> Lift:
            return Lift(n_c, n_h, r_c / n_c if n_c else 0.0, r_h / n_h if n_h else 0.0)

        pooled = lift(*(sum(r[i] for r in rows) for i in range(3, 7)))
        per_campaign = [{"campaign_id": r[0], "name": r[1], "sent_at": str(r[2]), **lift(*r[3:7]).as_dict()} for r in rows]
        clearly_positive = sum(1 for c in per_campaign if c["lift_95_low"] > 0)
        return {
            "campaign_type": campaign_type, "label": CAMPAIGN_TYPES[campaign_type]["label"],
            "synthetic": bool(self.synthetic), "as_of": str(self.as_of), "campaigns": len(rows),
            "pooled": pooled.as_dict(),
            "clearly_positive_campaigns": clearly_positive,
            "per_campaign": per_campaign,
            "reading": (f"Across {len(rows)} past {CAMPAIGN_TYPES[campaign_type]['label'].lower()} campaigns, contacted "
                        f"lines responded {pooled.points:+.2f} points versus their holdout (95% interval "
                        f"{pooled.interval[0]:+.2f} to {pooled.interval[1]:+.2f}); {clearly_positive} of {len(rows)} "
                        f"{'was' if clearly_positive == 1 else 'were'} clearly positive on their own. "
                        "Evidence from past campaigns, not a forecast."),
        }
