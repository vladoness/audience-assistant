"""Refinement: a plain-language change to an audience the marketer is already working on.

The model answers with a delta (REFINE_SCHEMA), never a regenerated definition, and the server
applies it here:
- a new condition replaces the changeable conditions on its attribute; remove_attributes drops them;
- governed conditions move only with their term, so a refinement can't half-edit a governed definition;
- open terms still waiting for an answer, and parts of the request with no data, carry forward.
Conditions the change doesn't mention are kept as they are, with their source, so an omission by
the model can't silently drop part of the audience. The result goes through the same validation,
status rules and approval checks as a new request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.catalog import Catalog, parse_pending
from app.explain import describe_condition
from app.governance import canonical
from app.models import AudienceDefinition, Changes, Condition, Issue


@dataclass
class RefinePlan:
    kept: list[Condition]
    proposal: dict[str, Any]  # proposal-shaped: the new conditions and terms, the channel, unmapped phrases
    pending_terms: list[str]
    issues: list[Issue] = field(default_factory=list)


def term_of(cond: Condition) -> str | None:
    if not cond.source.startswith("glossary:"):
        return None
    return cond.source.removeprefix("glossary:").partition("=")[0]


def answered_terms(definition: AudienceDefinition) -> set[str]:
    return {term_of(c) for c in definition.conditions if term_of(c) and "=" in c.source}


def model_state(definition: AudienceDefinition, pending_terms: list[str]) -> dict[str, Any]:
    """The current audience in the model's vocabulary: changeable conditions, governed terms by name."""
    return {
        "conditions": [{"attribute": c.attribute, "op": c.op, "value": c.value}
                       for c in definition.conditions if term_of(c) is None],
        "terms": list(dict.fromkeys(t for c in definition.conditions if (t := term_of(c)))),
        "pending_terms": pending_terms,
        "channel": definition.channel,
    }


def plan_refinement(current: AudienceDefinition, delta: dict[str, Any], pending_terms: list[str],
                    carried_unmapped: list[str]) -> RefinePlan:
    add = [c for c in delta.get("add_conditions") or [] if isinstance(c, dict)]
    replaced = {str(c.get("attribute")) for c in add} | {str(a) for a in delta.get("remove_attributes") or []}
    remove_terms = {str(t) for t in delta.get("remove_glossary_terms") or []}

    kept: list[Condition] = []
    for cond in current.conditions:
        term = term_of(cond)
        if term is not None:
            if term not in remove_terms:
                kept.append(cond)
        elif cond.attribute not in replaced:
            kept.append(cond)

    issues: list[Issue] = []
    governed_attrs = {c.attribute: term_of(c) for c in kept if term_of(c)}
    changeable_before = {c.attribute for c in current.conditions if term_of(c) is None}
    for attr in delta.get("remove_attributes") or []:
        if attr in governed_attrs and attr not in changeable_before:
            issues.append(Issue(
                level="warning", code="governed_condition", attribute=attr,
                message=f"That condition comes from the governed term '{governed_attrs[attr]}'. "
                        "Remove the term to drop it, or edit the conditions below."))
    for c in add:
        attr = str(c.get("attribute"))
        if attr in governed_attrs:
            issues.append(Issue(
                level="warning", code="overlaps_governed_term", attribute=attr,
                message=f"The governed term '{governed_attrs[attr]}' also sets this field. If your number should "
                        "replace it, remove that term's condition below."))

    in_use = {term_of(c) for c in kept if term_of(c)}
    answered = answered_terms(current)
    pending = [t for t in pending_terms if parse_pending(t)[0] not in remove_terms and parse_pending(t)[0] not in answered]
    new_terms = [t for t in dict.fromkeys(delta.get("add_glossary_terms") or [])
                 if t not in in_use and t not in {parse_pending(p)[0] for p in pending}]

    channel = delta.get("channel")
    if channel in (None, "", "unchanged"):
        channel = current.channel
    if delta.get("no_change") and not (add or replaced or remove_terms or new_terms or channel != current.channel):
        issues.append(Issue(level="warning", code="no_change",
                            message="That didn't read as a change to the audience, so nothing was changed."))

    unmapped = list(dict.fromkeys([*carried_unmapped, *(str(p) for p in delta.get("unmapped_phrases") or [])]))
    proposal = {
        "conditions": add,
        "glossary_terms": new_terms,
        "channel": channel,
        "unmapped_phrases": unmapped,
        "out_of_scope": False,
        "assumptions": delta.get("assumptions") or [],
    }
    return RefinePlan(kept=kept, proposal=proposal, pending_terms=pending, issues=issues)


def describe_changes(before: AudienceDefinition, after: AudienceDefinition, catalog: Catalog) -> Changes:
    def key(c: Condition) -> str:
        return json.dumps(canonical(c.model_dump()), sort_keys=True)

    old = {key(c): c for c in before.conditions}
    new = {key(c): c for c in after.conditions}
    return Changes(
        added=[describe_condition(c, catalog) for k, c in new.items() if k not in old],
        removed=[describe_condition(c, catalog) for k, c in old.items() if k not in new],
        channel=None if before.channel == after.channel else f"{before.channel.upper()} → {after.channel.upper()}",
    )
