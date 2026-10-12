"""Renders a validated definition as plain English, without the model.

The marketer approves this text, so it must describe exactly what the SQL will do. Generating
it from the definition (rather than asking the model to summarise) guarantees that.
"""

from __future__ import annotations

from app.catalog import Catalog
from app.models import AudienceDefinition, Condition, Explanation

_OP_PHRASES = {
    "eq": "is",
    "neq": "is not",
    "in": "is",
    "not_in": "is not",
    "gt": "is more than",
    "gte": "is at least",
    "lt": "is less than",
    "lte": "is at most",
}


def describe_condition(cond: Condition, catalog: Catalog) -> str:
    attr = catalog.attributes.get(cond.attribute)
    if attr is None:
        return f"{cond.attribute} {_OP_PHRASES.get(cond.op, cond.op)} {cond.value} (not a known field)"
    unit = f" {attr.unit}" if attr.unit else ""
    if attr.type == "boolean":
        return f"{attr.label}: {'yes' if cond.value is True else 'no' if cond.value is False else cond.value}"
    if cond.op == "between":
        if not (isinstance(cond.value, list) and len(cond.value) == 2):
            return f"{attr.label} is between {cond.value}{unit}"
        low, high = cond.value
        return f"{attr.label} is between {_fmt(low)} and {_fmt(high)}{unit}"
    if cond.op in {"in", "not_in"}:
        values = cond.value if isinstance(cond.value, list) else [cond.value]
        joined = " or ".join(str(v) for v in values) if cond.op == "in" else " nor ".join(str(v) for v in values)
        verb = "is" if cond.op == "in" else "is neither"
        if cond.op == "not_in" and len(values) == 1:
            verb, joined = "is not", str(values[0])
        return f"{attr.label} {verb} {joined}"
    if cond.value == 1 and attr.unit.endswith("s"):
        unit = f" {attr.unit[:-1]}"
    return f"{attr.label} {_OP_PHRASES[cond.op]} {_fmt(cond.value)}{unit}"


def explain(definition: AudienceDefinition, catalog: Catalog) -> Explanation:
    lines: list[str] = []
    governed: list[str] = []
    for cond in definition.conditions:
        text = describe_condition(cond, catalog)
        if cond.source.startswith("glossary:"):
            term_ref = cond.source.removeprefix("glossary:")
            term_name, _, option = term_ref.partition("=")
            term = catalog.terms.get(term_name)
            if term and term.status == "decided":
                tag = f"{term.name} — decision {term.decision.get('id')}, {term.decision.get('owner')}"
                for set_name in term.parameter_sets:
                    ps = catalog.parameter_sets[set_name]
                    tag += f"; parameters v{ps.version} from {ps.effective}"
            else:
                tag = f"{term_name} — your choice: {option}"
            text = f"{text}  [{tag}]"
            if term_ref not in governed:
                governed.append(term_ref)
        lines.append(text)

    action = catalog.action_for(definition.channel)
    channel_label = catalog.attributes[action.consent_attribute].label
    consent = f"Only lines with {channel_label} are included (applied automatically for {definition.channel.upper()} campaigns)."
    parts = [_lower_first(describe_condition(c, catalog)) for c in definition.conditions]
    if not parts:
        summary = f"All lines. {consent}"
    elif len(parts) == 1:
        summary = f"Lines where {parts[0]}. {consent}"
    else:
        summary = f"Lines where {', '.join(parts[:-1])} and {parts[-1]}. {consent}"
    return Explanation(summary=summary, lines=lines + [consent], governed_terms=governed)


def _fmt(value: float | int | str) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text
