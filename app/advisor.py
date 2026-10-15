"""Campaign advisor: once the audience is set, the model proposes improvements; code checks them.

1. `fact_sheet` computes what the system knows about this audience, as numbered facts with ids:
   size and consent, the mix by segment, the other channel, contact pressure, past lift of every
   campaign type, lift by segment for past campaigns of the chosen type (with a strict
   "clearly different" test), and overlap with recently approved audiences (by id, not name).
   Aggregates only: no customer row, no name typed by a marketer, and no count below SMALL_CELL
   reaches the model.
2. The model reads the facts and the goal and proposes up to three changes (ADVICE_SCHEMA).
3. `check` accepts a structured proposal only if every fact it cites exists and supports it, every
   number it uses appears in those facts, and its change is allowed (no policy-blocked field, no
   segment difference the data doesn't show, no campaign-type switch once the marketer chose one).
   Free-text "advice" and the summary are checked for citations and numbers only: code can't check
   what a sentence means. `consequence` applies a change hypothetically and sizes it. Withheld
   proposals are returned with the reason, never silently dropped.
4. The marketer applies one or ignores them. An applied change is a condition with source
   "advisor" and goes through the same validation and approval as any edit.

On AEP: a customer-built agent or tool next to Audience Agent, reading the same facts from CJA and the
audience estimate.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.audience import AudienceEngine
from app.catalog import Catalog
from app.history import SEGMENT_ATTRIBUTES, CampaignHistory
from app.models import AudienceDefinition, Condition
from app.synthdata import CAMPAIGN_TYPES
from app.store import definition_hash
from app.validate import validate_definition

import json
import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException

from app.llm import ProviderUnavailable
from app.pii import mask_pii

if TYPE_CHECKING:
    from app.main import Services

log = logging.getLogger("app")

KINDS = ("narrow_segment", "exclude_segment", "switch_channel", "choose_campaign_type", "advice")
FIELDS_BY_KIND = {"narrow_segment": {"attribute", "value"}, "exclude_segment": {"attribute", "value"},
                  "switch_channel": {"channel"}, "choose_campaign_type": {"campaign_type"}, "advice": set()}
SMALL_CELL = 10  # counts below this are reported as "fewer than 10" so no group small enough to identify leaves
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(text)}


def _with_roundings(numbers: set[str]) -> set[str]:
    """A fact's numbers plus the same numbers rounded (1.87 -> 1.9 -> 2), so honest rounding passes."""
    out = set(numbers)
    for n in numbers:
        if "." in n:
            v = float(n)
            out |= {f"{v:.1f}", f"{round(v):d}"}
    return out


def _signed(v: float) -> str:
    return f"{v:+.2f}"


def _count(n: int) -> str:
    return f"fewer than {SMALL_CELL}" if 0 < n < SMALL_CELL else f"{n:,}"


def _share(n: int, of: int) -> str:
    return f" ({n / of:.0%})" if of and n >= SMALL_CELL else ""


def _counts(values: list[int]) -> list[str]:
    """Counts that add up (a total and its parts): if any is suppressed, the others are rounded to tens,
    so the suppressed one can't be recovered by subtraction."""
    if not any(0 < n < SMALL_CELL for n in values):
        return [f"{n:,}" for n in values]
    return [_count(n) if n < SMALL_CELL else f"about {round(n, -1):,}" for n in values]


def fact_sheet(engine: AudienceEngine, catalog: Catalog, history: CampaignHistory | None, store,
               definition: AudienceDefinition, campaign_type: str | None) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []

    def add(fid: str, text: str, **data: Any) -> None:
        facts.append({"id": fid, "text": text, "data": data})

    channel = definition.channel
    sized = engine.size(definition)
    reachable, matched, excluded = _counts([sized.reachable, sized.matched, sized.excluded_no_consent])
    add("audience", f"The audience reaches {reachable} lines on {channel.upper()} ({matched} match the conditions; "
                    f"{excluded} have no {channel.upper()} consent).", reachable=sized.reachable)

    allowed = [a for a in SEGMENT_ATTRIBUTES if not catalog.violations(a, channel)]
    mix = engine.mix(definition, allowed)
    for attr, counts in mix.items():
        if len(counts) >= 2:
            label = catalog.attributes[attr].label
            shown = _counts(list(counts.values()))
            suppressed = any(0 < n < SMALL_CELL for n in counts.values())  # exact shares would undo the rounding
            parts = ", ".join(f"{v} {c}{'' if suppressed else _share(n, sized.reachable)}"
                              for (v, n), c in zip(counts.items(), shown))
            add(f"mix.{attr}", f"By {label.lower()}: {parts}.", values=counts)

    other = "email" if channel == "sms" else "sms"
    moved, problems = validate_definition(definition.model_copy(update={"channel": other}), catalog)
    blocked = [p for p in problems if p.level == "error"]
    if blocked:
        add(f"channel.{other}", f"On {other.upper()} this audience is not allowed: {blocked[0].message}", allowed=False)
    else:
        add(f"channel.{other}", f"On {other.upper()} it would reach {_count(engine.size(moved).reachable)} lines.",
            allowed=True, reachable=engine.size(moved).reachable)

    if history is not None and history.available:
        pressure = history.contact_pressure(definition)
        add("pressure.recent", f"{_count(pressure.contacted_recently)} of these lines"
                               f"{_share(pressure.contacted_recently, pressure.reachable)} got a campaign in the "
                               f"last {pressure.recent_days} days.", count=pressure.contacted_recently)
        add("pressure.cap", f"{_count(pressure.at_or_over_cap)} of these lines already had {pressure.cap} or more "
                            f"campaigns in the last {pressure.window_days} days.", count=pressure.at_or_over_cap)
        own = store.evidence_outcomes()
        for t in CAMPAIGN_TYPES:
            e = history.evidence(t, own)
            p = e["pooled"]
            add(f"type.{t}", f"Past {e['label'].lower()} campaigns ({e['campaigns']}, sent by "
                             f"{CAMPAIGN_TYPES[t]['channel'].upper()}): {_signed(p['lift_points'])} points "
                             f"against their holdout (95% {_signed(p['lift_95_low'])} to {_signed(p['lift_95_high'])}).",
                lift=p["lift_points"], low=p["lift_95_low"], high=p["lift_95_high"], channel=CAMPAIGN_TYPES[t]["channel"],
                eligible_share=engine.share_where(definition, CAMPAIGN_TYPES[t]["audience"]))
        for t in CAMPAIGN_TYPES:
            for attr in allowed:
                present = mix.get(attr, {})
                if len(present) < 2:
                    continue
                for seg in history.segment_lift(t, attr):
                    if seg["value"] not in present or (t != campaign_type and not seg["clearly_different"]):
                        continue
                    verdict = "clearly different" if seg["clearly_different"] else "not clearly different"
                    add(f"segment.{t}.{attr}.{seg['value']}",
                        f"Past {CAMPAIGN_TYPES[t]['label'].lower()} campaigns, {seg['value']} lines: "
                        f"{_signed(seg['lift_points'])} points (95% {_signed(seg['lift_95_low'])} to "
                        f"{_signed(seg['lift_95_high'])}) against {_signed(seg['rest_lift_points'])} for the other lines; "
                        f"{verdict}.",
                        campaign_type=t, attribute=attr, value=seg["value"], difference=seg["difference_points"],
                        clearly_different=seg["clearly_different"], lift=seg["lift_points"])

    for o in recent_overlaps(engine, store, definition):
        add(f"overlap.{o['audience_id']}", f"{_count(o['shared'])} of these lines{_share(o['shared'], o['of'])} are also "
                                           f"in approved audience #{o['audience_id']} (approved on {o['approved_on']}).",
            shared=o["shared"])
    return facts


def recent_overlaps(engine: AudienceEngine, store, definition: AudienceDefinition, days: int = 14,
                    limit: int = 20) -> list[dict[str, Any]]:
    """Reachable lines this audience shares with audiences approved in the last `days`, largest first.
    Overlapping audiences send to the same people and blur each other's holdouts."""
    mine = engine.profile_ids(definition)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = []
    for a in store.list_audiences(limit=limit):
        if a["created_at"] < since or not mine:
            continue
        other = AudienceDefinition.model_validate_json(a["definition_json"])
        shared = len(mine & engine.profile_ids(other))
        if shared:
            out.append({"audience_id": a["id"], "approved_on": a["created_at"][:10], "shared": shared, "of": len(mine),
                        "share": round(shared / len(mine), 3), "same_definition": a["definition_hash"] == definition_hash(
                            definition.model_dump())})
    return sorted(out, key=lambda o: -o["shared"])


def overlap_note(overlaps: list[dict[str, Any]], threshold: float = 0.25) -> str | None:
    """A warning when a large share of this audience was approved recently in another audience."""
    big = [o for o in overlaps if o["share"] >= threshold]
    if not big:
        return None
    o = big[0]
    same = " with the same definition" if o["same_definition"] else ""
    more = f" (and {len(big) - 1} more)" if len(big) > 1 else ""
    return (f"{o['share']:.0%} of these lines are also in audience #{o['audience_id']}, approved on {o['approved_on']}"
            f"{same}{more}. Lines held out of one can be contacted by the other, which blurs both measurements: "
            "send it as the same campaign, or wait until the other one's window has closed.")


def check_summary(summary: str, facts: dict[str, dict[str, Any]]) -> str | None:
    """The summary may only use numbers that appear somewhere in the facts."""
    allowed = _with_roundings(set().union(*(_numbers(f["text"]) for f in facts.values())))
    used = _numbers(summary) - allowed
    return f"it uses numbers that aren't in the facts ({', '.join(sorted(used))})" if used else None


def check(proposal: dict[str, Any], facts: dict[str, dict[str, Any]], catalog: Catalog,
          definition: AudienceDefinition, campaign_type: str | None) -> str | None:
    """None if the proposal is supported by the facts it cites; otherwise the reason it is withheld."""
    kind = proposal.get("kind")
    cites = [str(c) for c in proposal.get("cites") or []]
    if kind not in KINDS:
        return "it isn't a kind of change the advisor may propose"
    if not cites or any(c not in facts for c in cites):
        return "it cites facts the system didn't provide"
    allowed_numbers = _with_roundings(set().union(*(_numbers(facts[c]["text"]) for c in cites)))
    used = _numbers(f"{proposal.get('title', '')} {proposal.get('rationale', '')}") - allowed_numbers
    if used:
        return f"it uses numbers that aren't in the facts it cites ({', '.join(sorted(used))})"

    if kind in ("narrow_segment", "exclude_segment"):
        attr, value = proposal.get("attribute"), proposal.get("value")
        fid = f"segment.{campaign_type}.{attr}.{value}"
        if attr not in SEGMENT_ATTRIBUTES or catalog.violations(attr, definition.channel):
            return f"it uses a field the advisor may not use for {definition.channel.upper()} campaigns"
        if fid not in cites:
            return "it doesn't cite past results for that segment under this campaign type"
        seg = facts[fid]["data"]
        if not seg["clearly_different"]:
            return "past campaigns don't show that segment responding clearly differently"
        if (kind == "narrow_segment") != (seg["difference"] > 0):
            return "the direction contradicts the facts (that segment responds worse, not better, or the reverse)"
    elif kind == "switch_channel":
        target = proposal.get("channel")
        fid = f"channel.{target}"
        if target == definition.channel or fid not in facts:
            return "there is no other channel fact for that switch"
        if fid not in cites or not facts[fid]["data"]["allowed"]:
            return "that channel isn't allowed for this audience"
    elif kind == "choose_campaign_type":
        t = proposal.get("campaign_type")
        if campaign_type:
            return ("you chose the campaign type; a different type is a different offer, which is your decision, "
                    "not a change to the audience")
        if t not in CAMPAIGN_TYPES or f"type.{t}" not in cites:
            return "it doesn't cite past results for that campaign type"
        d = facts[f"type.{t}"]["data"]
        if d["channel"] != definition.channel:
            return f"past campaigns of that type were sent by {d['channel'].upper()}, not {definition.channel.upper()}"
        if not d["low"] > 0:
            return "past results for that type can't be told apart from no effect"
        if d.get("eligible_share", 1.0) < 0.5:
            return "most of this audience isn't eligible for that campaign type (for example, a prepaid-only offer)"
    return None


def consequence(proposal: dict[str, Any], facts: dict[str, dict[str, Any]], engine: AudienceEngine, catalog: Catalog,
                definition: AudienceDefinition, campaign_type: str | None, holdout_share: float = 0.1) -> dict[str, Any]:
    """What applying the proposal would do, computed by code: the new definition and its size."""
    kind = proposal["kind"]
    before = engine.size(definition).reachable
    if kind in ("narrow_segment", "exclude_segment"):
        attr, value = proposal["attribute"], proposal["value"]
        kept = [c for c in definition.conditions if not (kind == "narrow_segment" and c.attribute == attr
                                                         and not c.source.startswith("glossary:"))]
        op = "eq" if kind == "narrow_segment" else "neq"
        new = definition.model_copy(update={"conditions": [*kept, Condition(attribute=attr, op=op, value=value,
                                                                              source="advisor")]})
        new, _ = validate_definition(new, catalog)
        after = engine.size(new).reachable
        # Expected extra responders, from each segment's past lift: the rate goes up when you narrow, but the
        # total can go down. Contacted lines exclude the holdout.
        mix = facts.get(f"mix.{attr}", {}).get("data", {}).get("values", {})
        lift = {v: facts[f"segment.{campaign_type}.{attr}.{v}"]["data"]["lift"] for v in mix
                if f"segment.{campaign_type}.{attr}.{v}" in facts}
        contacted_share = 1 - holdout_share
        keep = (lambda v: v == value) if kind == "narrow_segment" else (lambda v: v != value)
        extra_before = sum(n * contacted_share * lift[v] / 100 for v, n in mix.items() if v in lift)
        extra_after = sum(n * contacted_share * lift[v] / 100 for v, n in mix.items() if v in lift and keep(v))
        rate_after = extra_after / (contacted_share * after) * 100 if after else 0.0
        rate_before = extra_before / (contacted_share * before) * 100 if before else 0.0
        note = (f"Reaches {after:,} lines instead of {before:,}. From past results, about {extra_after:,.0f} extra "
                f"responders instead of {extra_before:,.0f} (rate {_signed(rate_after)} instead of {_signed(rate_before)} "
                f"points): {'a better rate, fewer responders in total' if extra_after < extra_before else 'more in total'}.")
        return {"definition": new.model_dump(), "reachable_before": before, "reachable_after": after, "note": note,
                "extra_responders_before": round(extra_before), "extra_responders_after": round(extra_after)}
    if kind == "switch_channel":
        new = definition.model_copy(update={"channel": proposal["channel"]})
        after = engine.size(new).reachable
        return {"definition": new.model_dump(), "reachable_before": before, "reachable_after": after,
                "note": f"Reaches {after:,} lines on {proposal['channel'].upper()} instead of {before:,}."}
    if kind == "choose_campaign_type":
        t = proposal["campaign_type"]
        d = facts[f"type.{t}"]["data"]
        return {"campaign_type": t, "note": f"Past {CAMPAIGN_TYPES[t]['label'].lower()} campaigns: {_signed(d['lift'])} "
                                            f"points against their holdout."}
    return {}


def run_advice(s: Services, definition: AudienceDefinition, goal: str, campaign_type: str | None,
               request_id: str | None = None) -> dict:
    """Facts by code, proposals by the model, checks and consequences by code. Shared by the API and MCP."""
    checked = s.interpreter.check(definition, [], {})
    if checked.status == "invalid":
        raise HTTPException(422, detail="Fix the audience's errors before asking for advice.")
    if s.engine.size(checked.definition).reachable < s.engine.min_audience:
        raise HTTPException(422, detail=f"The audience is too small for advice (fewer than {s.engine.min_audience} "
                                        "reachable lines): past results can't say anything about it.")
    if campaign_type is not None and campaign_type not in CAMPAIGN_TYPES:
        raise HTTPException(422, detail=f"Unknown campaign type {campaign_type!r}.")
    facts = fact_sheet(s.engine, s.catalog, s.history, s.store, checked.definition, campaign_type)
    try:
        raw, tel = s.interpreter.advise(facts, goal, campaign_type)
    except ProviderUnavailable as exc:
        raise HTTPException(503, detail=f"The advisor is unavailable ({exc}).") from None
    by_id = {f["id"]: f for f in facts}
    holdout = s.catalog.parameter_sets.get("measurement")
    share = float(holdout.values["holdout_share"]) if holdout else 0.1
    accepted, rejected = [], []
    for p in (raw.get("proposals") or [])[:3]:
        reason = check(p, by_id, s.catalog, checked.definition, campaign_type)
        if reason:
            rejected.append({"title": str(p.get("title", ""))[:200], "kind": p.get("kind"), "reason": reason})
        else:
            used = FIELDS_BY_KIND[p["kind"]]  # blank what a kind doesn't use: no free text rides along
            accepted.append({"kind": p["kind"], "title": str(p["title"])[:200], "rationale": str(p["rationale"])[:600],
                             "cites": p["cites"]}
                            | {k: (p[k] if k in used else ("none" if k == "channel" else "")) for k in
                               ("attribute", "value", "channel", "campaign_type")}
                            | {"cited_facts": [by_id[c]["text"] for c in p["cites"]],
                               "consequence": consequence(p, by_id, s.engine, s.catalog, checked.definition,
                                                                  campaign_type, holdout_share=share)})
    summary = str(raw.get("summary", ""))[:600]
    summary_problem = check_summary(summary, by_id)
    if summary_problem:
        rejected.append({"title": "Summary", "kind": "summary", "reason": summary_problem})
        summary = ""
    s.metrics.incr("advice_accepted", len(accepted))
    s.metrics.incr("advice_rejected", len(rejected))
    advice_id = s.store.record_advice({
        "request_id": request_id, "campaign_type": campaign_type, "goal_masked": mask_pii(goal.strip()[: s.settings.max_request_chars])[0],
        "facts_json": json.dumps([{"id": f["id"], "text": f["text"]} for f in facts]), "raw_json": json.dumps(raw),
        "accepted_json": json.dumps(accepted), "rejected_json": json.dumps(rejected),
        "model": tel.model, "prompt_version": tel.prompt_version})
    log.info("advice", extra={"advice_id": advice_id, "accepted": [p["kind"] for p in accepted],
                              "rejected": [r["reason"] for r in rejected], "facts": len(facts),
                              "campaign_type": campaign_type})
    return {"advice_id": advice_id, "summary": summary, "proposals": accepted, "rejected": rejected,
            "facts": [{"id": f["id"], "text": f["text"]} for f in facts], "telemetry": tel.model_dump(),
            "synthetic_history": bool(s.history and s.history.synthetic),
            "model_written": ["summary", "proposals[].title", "proposals[].rationale", "rejected[].title"]}
