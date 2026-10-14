"""Campaign advisor: once the audience is set, the model proposes improvements; code checks them.

1. `fact_sheet` computes what the system knows about this audience, as numbered facts with ids:
   size and consent, the mix by segment, the other channel, contact pressure, past lift of every
   campaign type, lift by segment for past campaigns of the chosen type (with a strict
   "clearly different" test), and overlap with recently approved audiences. Aggregates only;
   no customer row reaches the model.
2. The model reads the facts and the goal and proposes up to three changes (ADVICE_SCHEMA).
3. `check` accepts a proposal only if every fact it cites exists and supports it, every number it
   uses appears in those facts, and its change is allowed (no policy-blocked field, no segment
   difference the data doesn't show). `consequence` then applies the change hypothetically and
   sizes it. Rejected proposals are returned with the reason, never silently dropped.
4. The marketer applies one or ignores them. An applied change is a condition with source
   "advisor" and goes through the same validation and approval as any edit.

Ontology contract (target): POST /v1/ontology/{oid}/actions/adviseAudience over the same facts.
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
from app.validate import validate_definition

KINDS = ("narrow_segment", "exclude_segment", "switch_channel", "choose_campaign_type", "advice")
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


def fact_sheet(engine: AudienceEngine, catalog: Catalog, history: CampaignHistory | None, store,
               definition: AudienceDefinition, campaign_type: str | None) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []

    def add(fid: str, text: str, **data: Any) -> None:
        facts.append({"id": fid, "text": text, "data": data})

    channel = definition.channel
    sized = engine.size(definition)
    add("audience", f"The audience reaches {sized.reachable:,} lines on {channel.upper()} ({sized.matched:,} match the "
                    f"conditions; {sized.excluded_no_consent:,} have no {channel.upper()} consent).",
        reachable=sized.reachable)

    allowed = [a for a in SEGMENT_ATTRIBUTES if not catalog.violations(a, channel)]
    mix = engine.mix(definition, allowed)
    for attr, counts in mix.items():
        if len(counts) >= 2:
            label = catalog.attributes[attr].label
            parts = ", ".join(f"{v} {n:,} ({n / sized.reachable:.0%})" for v, n in counts.items())
            add(f"mix.{attr}", f"By {label.lower()}: {parts}.", values=counts)

    other = "email" if channel == "sms" else "sms"
    moved, problems = validate_definition(definition.model_copy(update={"channel": other}), catalog)
    blocked = [p for p in problems if p.level == "error"]
    if blocked:
        add(f"channel.{other}", f"On {other.upper()} this audience is not allowed: {blocked[0].message}", allowed=False)
    else:
        add(f"channel.{other}", f"On {other.upper()} it would reach {engine.size(moved).reachable:,} lines.",
            allowed=True, reachable=engine.size(moved).reachable)

    if history is not None and history.available:
        pressure = history.contact_pressure(definition)
        add("pressure.recent", f"{pressure.contacted_recently:,} of these lines "
                               f"({pressure.contacted_recently / max(pressure.reachable, 1):.0%}) got a campaign in the "
                               f"last {pressure.recent_days} days.", count=pressure.contacted_recently)
        add("pressure.cap", f"{pressure.at_or_over_cap:,} of these lines already had {pressure.cap} or more campaigns in "
                            f"the last {pressure.window_days} days.", count=pressure.at_or_over_cap)
        own = store.outcomes()
        for t in CAMPAIGN_TYPES:
            e = history.evidence(t, own)
            p = e["pooled"]
            add(f"type.{t}", f"Past {e['label'].lower()} campaigns ({e['campaigns']}): {_signed(p['lift_points'])} points "
                             f"against their holdout (95% {_signed(p['lift_95_low'])} to {_signed(p['lift_95_high'])}).",
                lift=p["lift_points"], low=p["lift_95_low"], high=p["lift_95_high"])
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

    mine = engine.profile_ids(definition)
    since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    for a in store.list_audiences(limit=20):
        if a["created_at"] < since or not mine:
            continue
        shared = len(mine & engine.profile_ids(AudienceDefinition.model_validate_json(a["definition_json"])))
        if shared:
            add(f"overlap.{a['id']}", f"{shared:,} of these lines ({shared / len(mine):.0%}) are also in approved audience "
                                      f"#{a['id']} \"{a['name']}\".", shared=shared)
    return facts


def check(proposal: dict[str, Any], facts: dict[str, dict[str, Any]], catalog: Catalog,
          definition: AudienceDefinition, campaign_type: str | None) -> str | None:
    """None if the proposal is supported by the facts it cites; otherwise the reason it is withheld."""
    kind = proposal.get("kind")
    cites = [str(c) for c in proposal.get("cites") or []]
    if kind not in KINDS:
        return f"unknown kind {kind!r}"
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
            return f"{attr!r} isn't a field the advisor may use for {definition.channel.upper()} campaigns"
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
        if t not in CAMPAIGN_TYPES or t == campaign_type or f"type.{t}" not in cites:
            return "it doesn't cite past results for that campaign type"
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
