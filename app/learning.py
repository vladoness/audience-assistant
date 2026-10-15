"""The learning loop, as code: how a decision will be judged, and what happened.

- `measurement_plan`: at approval, the goal, the holdout, the evidence the approver saw, whether the
  result will be readable (`detectable_lift`), and overlap with recent approvals.
- `recheck_consent`: consent is applied up to the hand-off (the first export of the lines to contact),
  never after it.
- `record_outcome` / `outcome_view`: a reported outcome is checked, counted against the groups as
  handed off, stored as pending, and shown against what was expected.

Route handlers in app/main.py call these; nothing here knows about HTTP beyond raising HTTPException.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import HTTPException

from app import advisor
from app.history import counts_lift, verdict
from app.models import AudienceDefinition
from app.synthdata import CAMPAIGN_TYPES

if TYPE_CHECKING:
    from app.main import Services

log = logging.getLogger("app")


def record_outcome(s: Services, audience_id: int, responders: list[str], source: str, sent_at: str,
                   reporter: dict) -> dict:
    """Count responders in the contacted and held-out groups recorded at approval, measure the lift, and
    store it as pending. A report that can't be right is rejected before it can reach the evidence: sent
    before the approval or in the future, still inside the measurement window, mostly lines that aren't in
    this audience, or a response rate no campaign has."""
    row = s.store.get_audience(audience_id)
    if row is None:
        raise HTTPException(404, detail="Unknown audience.")
    m = json.loads(row["measurement_json"])
    if "split" not in m:
        raise HTTPException(422, detail="This audience was approved without a holdout, so its lift can't be measured.")
    ps = s.catalog.parameter_sets.get("measurement")
    max_rate = float(ps.values.get("max_response_rate", 1.0)) if ps else 1.0
    approved_on = datetime.fromisoformat(row["created_at"]).astimezone().date()  # stored in UTC; compared locally
    today, sent = s.today(), date.fromisoformat(sent_at)
    if not approved_on <= sent <= today:
        raise HTTPException(422, detail=f"sent_at {sent_at} must be between the approval ({approved_on}) and today.")
    if source == "campaign_tool" and today < sent + timedelta(days=m["window_days"]):
        raise HTTPException(422, detail=(f"The {m['window_days']}-day measurement window is open until "
                                         f"{sent + timedelta(days=m['window_days'])}; report after that."))
    s.store.record_handoff(audience_id, f"outcome report ({source})")  # the send has happened by now
    groups = dict(s.store.members(audience_id))  # as handed off: consent was applied at export, before the send
    responded = set(responders)
    contacted = sum(1 for g in groups.values() if g == "contacted")
    held_out = len(groups) - contacted
    contacted_r = sum(1 for p in responded if groups.get(p) == "contacted")
    held_out_r = sum(1 for p in responded if groups.get(p) == "held_out")
    unknown = len(responded) - contacted_r - held_out_r
    if responded and unknown > len(responded) / 2:
        raise HTTPException(422, detail=(f"{unknown:,} of {len(responded):,} reported responders are not in this "
                                         "audience: is this the right file?"))
    for name, r, n in (("contacted", contacted_r, contacted), ("held-out", held_out_r, held_out)):
        if n and r / n > max_rate:
            raise HTTPException(422, detail=(f"{r / n:.0%} of the {name} lines responding is above the plausible "
                                             f"maximum ({max_rate:.0%}, measurement v{ps.version})."))
    stored = s.store.report_outcome({
        "audience_id": audience_id, "source": source, "campaign_type": m.get("campaign_type"), "sent_at": sent_at,
        "contacted": contacted, "contacted_responders": contacted_r, "held_out": held_out,
        "held_out_responders": held_out_r, "unknown_responders": unknown,
        "membership_matches": int(current_data_matches(s, row)), "reporter_json": json.dumps(reporter),
    })
    if not stored:
        raise HTTPException(409, detail="This audience's outcome is already confirmed; it can't be replaced.")
    s.metrics.incr(f"outcomes_{source}")
    result = {"audience_id": audience_id, "campaign_type": m.get("campaign_type"),
              **outcome_view(s, s.store.outcomes(include_pending=True, audience_id=audience_id)[0], row["measurement_json"])}
    log.info("outcome_reported", extra={k: result[k] for k in ("audience_id", "source", "campaign_type", "verdict")}
             | {"lift_points": result["measured"]["lift_points"], "unknown_responders": unknown})
    return result


def outcome_view(s: Services, o: dict, measurement_json: str) -> dict:
    """A stored outcome as the UI and API show it: measured against what was expected at approval."""
    expected = json.loads(measurement_json).get("evidence_at_approval")
    ps = s.catalog.parameter_sets.get("measurement")
    measured = counts_lift(o["contacted"], o["held_out"], o["contacted_responders"], o["held_out_responders"])
    judged = verdict(expected, measured, int(ps.values["min_holdout_lines"]) if ps else 0)
    return {"source": o["source"], "sent_at": o["sent_at"], "status": o["status"], "reports": o["reports"],
            "confirmed_by": o["confirmed_by"], "measured": measured.as_dict(), "expected": expected,
            "verdict": judged,
            # A pending result that clearly differs from the evidence may be real, or a wrong file: say so.
            "check_before_confirming": o["status"] == "pending" and judged.startswith(("below", "above")),
            "kept_out_of_evidence": json.loads(measurement_json).get("overlap_note"),
            "unknown_responders": o["unknown_responders"],
            "current_data_matches_approval": bool(o["membership_matches"])}


def recheck_consent(s: Services, audience_id: int, row: dict) -> int:
    """Membership is frozen at approval, consent isn't: up to the hand-off (the first export of the lines to
    contact), lines that lost consent for the channel, or left the profile store, are left out of both
    groups. Never after it: only contacted lines can opt out because of the campaign, so dropping them then
    would inflate the measured lift. Returns how many lines have been left out since approval."""
    if s.store.handed_off_at(audience_id):
        return s.store.excluded_count(audience_id)
    gone = s.engine.without_consent([pid for pid, _ in s.store.members(audience_id)], row["channel"])
    if gone:
        s.store.exclude_members(audience_id, gone, f"no {row['channel']} consent on {s.today().isoformat()}")
        s.metrics.incr("members_excluded_no_consent", len(gone))
        log.warning("members_excluded_no_consent", extra={"audience_id": audience_id, "lines": len(gone)})
    return s.store.excluded_count(audience_id)


def current_data_matches(s: Services, row: dict) -> bool:
    """Would today's profile data still put exactly the same lines in each group?"""
    m = json.loads(row["measurement_json"])
    if "split" not in m:
        return False
    definition = AudienceDefinition(**json.loads(row["definition_json"]))
    return m.get("members_sha") == s.engine.member_digests(definition, m["split"]["salt"], m["holdout_share"])


def measurement_plan(s: Services, definition: AudienceDefinition, goal: str, salt: str,
                     campaign_type: str | None = None) -> dict:
    """How this decision will be judged later: the campaign goal, a governed share of the audience held out
    (not contacted), and the comparison that says whether it worked. Recorded with the approval, together
    with the evidence the approver had: past lift for this campaign type and the audience's contact pressure."""
    ps = s.catalog.parameter_sets.get("measurement")
    share = float(ps.values["holdout_share"]) if ps else 0.0
    window = int(ps.values["window_days"]) if ps else 0
    target, holdout = s.engine.split(definition, salt, share)
    measurable = ps is not None and holdout >= int(ps.values["min_holdout_lines"])
    past = s.history.evidence(campaign_type, s.store.evidence_outcomes()) if s.history is not None and campaign_type else None
    pressure = s.history.contact_pressure(definition) if s.history is not None else None
    detectable, power_note = detectable_lift(past, target, holdout)
    overlaps = advisor.recent_overlaps(s.engine, s.store, definition)
    return {
        "goal": goal,
        "campaign_type": campaign_type,
        "evidence_at_approval": None if past is None else {
            "synthetic": past["synthetic"], "as_of": past["as_of"], "campaigns": past["campaigns"], **past["pooled"]},
        "contact_pressure_at_approval": None if pressure is None else pressure.model_dump(exclude={"warnings"}),
        "success_measure": (f"Response rate of the {target:,} contacted lines minus that of the {holdout:,} held-out "
                            f"lines, over {window} days after the send"
                            + (f" (a response: the line {CAMPAIGN_TYPES[campaign_type]['response']})."
                               if campaign_type else ".")),
        "target_lines": target,
        "holdout_lines": holdout,
        "holdout_share": share,
        "window_days": window,
        "measurable": measurable,
        "note": None if measurable else "The holdout is too small for the result to be reliable; widen the audience "
                                        "or treat the outcome as anecdotal.",
        "detectable_lift_points": detectable,
        "power_note": power_note,
        "overlaps_at_approval": overlaps[:3],
        "overlap_note": advisor.overlap_note(overlaps),
        "split": {"method": "md5_number_lower(salt || ':' || profile_id) % 10000 < share x 10000", "salt": salt},
        "parameters": f"measurement v{ps.version}" if ps else None,
        "data_snapshot": s.engine.snapshot,
        "members_sha": s.engine.member_digests(definition, salt, share),
    }


def detectable_lift(past: dict | None, target: int, holdout: int) -> tuple[float | None, str | None]:
    """The smallest lift this split would show clearly (80% of the time, at 95%: z 1.96 + 0.84), at the
    campaign type's past base rate, and a warning when past campaigns of the type lifted less than that."""
    if past is None or not target or not holdout:
        return None, None
    p = past["pooled"]["held_out_response_pct"] / 100
    detectable = round(100 * 2.8 * math.sqrt(p * (1 - p) * (1 / target + 1 / holdout)), 2)
    if past["pooled"]["lift_points"] >= detectable:
        return detectable, None
    return detectable, (f"With {holdout:,} lines held out, only a lift of about {detectable:.2f} points or more would "
                        f"show clearly; past campaigns of this type lifted {past['pooled']['lift_points']:+.2f}, so this "
                        "one will probably read \"too small to tell\". A wider audience would fix it; the holdout "
                        "share is a governed setting (Marketing Analytics).")
