"""Change impact for approved audiences.

At approval we snapshot which governed terms an audience relies on and the decision and
parameter versions in force. Later, comparing that snapshot with the current configuration says
which approved audiences a glossary or parameter change affects, and what each would select
under the new definition, before anyone activates a stale audience.
"""

from __future__ import annotations

import json
from typing import Any

from app.catalog import Catalog
from app.models import AudienceDefinition, Condition


def verify_provenance(definition: AudienceDefinition, catalog: Catalog) -> AudienceDefinition:
    """Keep a "glossary:" source only when the condition is exactly that term's (or option's) current
    definition. Anything else is relabelled "user", so a client can't make an edited threshold look governed."""
    out = []
    for c in definition.conditions:
        if c.source not in {"model", "user"} and not c.source.startswith("glossary:"):
            c = c.model_copy(update={"source": "user"})
        if c.source.startswith("glossary:"):
            term_name, _, option = c.source.removeprefix("glossary:").partition("=")
            term = catalog.terms.get(term_name)
            expected: tuple[dict[str, Any], ...] = ()
            if term is not None and option:
                chosen = term.option(option)
                expected = chosen.conditions if chosen else ()
            elif term is not None and term.status == "decided":
                expected = term.conditions
            if _canon(c.model_dump()) not in [_canon(e) for e in expected]:
                c = c.model_copy(update={"source": "user"})
        out.append(c)
    return definition.model_copy(update={"conditions": out})


def reconcile_with_proposal(final: AudienceDefinition, proposed: AudienceDefinition) -> tuple[AudienceDefinition, bool]:
    """Server-side truth for "model" sources, and whether the marketer edited the AI's proposal.

    A condition counts as the model's only if the model actually proposed it. Answering a
    clarification is not an edit; changing the channel or adding, removing or changing any other
    condition (including a governed one the grounding check warned about) is.
    """
    model_keys = {_key(c) for c in proposed.conditions if c.source == "model"}
    conditions = [
        c.model_copy(update={"source": "user"}) if c.source == "model" and _key(c) not in model_keys else c
        for c in final.conditions
    ]

    def targeting(d: AudienceDefinition) -> set[str]:
        return {_key(c) for c in d.conditions if not (c.source.startswith("glossary:") and "=" in c.source)}

    edited = final.channel != proposed.channel or targeting(final) != targeting(proposed)
    return final.model_copy(update={"conditions": conditions}), edited


def _key(c: Condition) -> str:
    return json.dumps(_canon(c.model_dump()), sort_keys=True)


def snapshot(definition: AudienceDefinition, catalog: Catalog) -> dict[str, Any]:
    """Governed terms this definition depends on, with the versions in force right now."""
    snap: dict[str, Any] = {}
    for c in definition.conditions:
        if not c.source.startswith("glossary:"):
            continue
        term_name, _, option = c.source.removeprefix("glossary:").partition("=")
        term = catalog.terms.get(term_name)
        entry = snap.setdefault(term_name, {"conditions": []})
        entry["conditions"].append(_canon(c.model_dump()))
        if option:
            entry["option"] = option
        elif term is not None:
            entry["decision"] = term.decision.get("id")
            entry["parameters"] = {s: catalog.parameter_sets[s].version for s in term.parameter_sets}
    return snap


def impact(snap: dict[str, Any], catalog: Catalog) -> list[str]:
    """Human-readable changes to the governed definitions an approved audience relied on."""
    changes = []
    for term_name, entry in snap.items():
        term = catalog.terms.get(term_name)
        if term is None:
            changes.append(f"{term_name}: removed from the glossary")
            continue
        if "option" in entry:
            if term.status == "decided":
                changes.append(f"{term_name}: now decided ({term.decision.get('id')}); this audience used an interim choice")
            continue
        version_changes = [
            f"{set_name} v{old} → v{catalog.parameter_sets[set_name].version}"
            for set_name, old in entry.get("parameters", {}).items()
            if set_name in catalog.parameter_sets and catalog.parameter_sets[set_name].version != old
        ]
        current = sorted(json.dumps(_canon(c), sort_keys=True) for c in term.conditions)
        stored = sorted(json.dumps(c, sort_keys=True) for c in entry["conditions"])
        if version_changes:
            changes.append(f"{term_name}: parameters {', '.join(version_changes)}")
        elif current != stored:
            changes.append(f"{term_name}: definition changed (decision {term.decision.get('id')})")
    return changes


def reexpand(definition: AudienceDefinition, catalog: Catalog) -> AudienceDefinition:
    """The same audience with every decided governed term replaced by its current definition."""
    kept = []
    replaced: set[str] = set()
    for c in definition.conditions:
        term_name, _, option = c.source.removeprefix("glossary:").partition("=")
        term = catalog.terms.get(term_name) if c.source.startswith("glossary:") and not option else None
        if term is None or term.status != "decided":
            kept.append(c)
        elif term_name not in replaced:
            replaced.add(term_name)
            kept += [Condition(**tc, source=f"glossary:{term_name}") for tc in term.conditions]
    return definition.model_copy(update={"conditions": kept})


def _canon(c: dict[str, Any]) -> dict[str, Any]:
    def num(v: Any) -> Any:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

    value = c["value"]
    value = [num(v) for v in value] if isinstance(value, list) else num(value)
    return {"attribute": c["attribute"], "op": c["op"], "value": value}
