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
