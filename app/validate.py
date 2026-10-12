"""Deterministic checks that every audience definition must pass before it is sized or approved.

The model's output is treated as an untrusted proposal. This module decides what is valid:
fields must exist in the catalog, operators must fit the field type, values must be in range,
no enabled data-usage policy may forbid a field for the audience's marketing action, and the
conditions must not contradict each other or the scope of the fields they use.
"""

from __future__ import annotations

import math
from typing import Any

from app.catalog import Attribute, Catalog
from app.models import AudienceDefinition, Condition, Issue

MAX_LIST_VALUES = 50

OPS_BY_TYPE = {
    "enum": {"eq", "neq", "in", "not_in"},
    "integer": {"eq", "neq", "gt", "gte", "lt", "lte", "between"},
    "number": {"eq", "neq", "gt", "gte", "lt", "lte", "between"},
    "boolean": {"eq"},
}


def parse_condition(raw: Any, source: str = "model") -> tuple[Condition | None, Issue | None]:
    """Turn a loosely-typed dict (from the model or the UI) into a Condition, or report why not."""
    if not isinstance(raw, dict) or not {"attribute", "op", "value"} <= raw.keys():
        return None, Issue(level="error", code="malformed_condition", message=f"Malformed condition: {raw!r}")
    if raw["op"] not in {op for ops in OPS_BY_TYPE.values() for op in ops}:
        return None, Issue(
            level="error", code="unknown_operator", attribute=str(raw["attribute"]),
            message=f"Unknown operator '{raw['op']}'.",
        )
    return Condition(attribute=str(raw["attribute"]), op=raw["op"], value=raw["value"], source=source), None


def normalize(cond: Condition, catalog: Catalog, channel: str) -> tuple[Condition | None, list[Issue]]:
    """Check one condition against the catalog and policies, and coerce its value to the field's type."""
    attr = catalog.attributes.get(cond.attribute)
    if attr is None:
        return None, [_err("unknown_attribute", f"'{cond.attribute}' is not in the catalog.", cond.attribute)]
    violated = catalog.violations(attr.name, channel)
    if violated:
        policy = violated[0]
        label = next(lb for lb in attr.labels if lb in policy.deny_labels)
        return None, [_err(
            "policy_violation",
            f"{attr.label} can't be used for {channel} campaigns: policy {policy.id} ({policy.name}). {catalog.labels[label]}",
            attr.name,
        )]
    return normalize_shape(cond, catalog)


def normalize_shape(cond: Condition, catalog: Catalog) -> tuple[Condition | None, list[Issue]]:
    """Field exists, operator fits the type, value coerces and is in range. No policy check."""
    attr = catalog.attributes.get(cond.attribute)
    if attr is None:
        return None, [_err("unknown_attribute", f"'{cond.attribute}' is not in the catalog.", cond.attribute)]
    if cond.op not in OPS_BY_TYPE[attr.type]:
        allowed = ", ".join(sorted(OPS_BY_TYPE[attr.type]))
        return None, [_err("operator_not_allowed", f"'{cond.op}' does not apply to {attr.label} (allowed: {allowed}).", attr.name)]
    try:
        value = _coerce_value(attr, cond.op, cond.value)
    except ValueError as exc:
        return None, [_err("invalid_value", f"{attr.label}: {exc}", attr.name)]
    return cond.model_copy(update={"value": value}), []


def validate_definition(definition: AudienceDefinition, catalog: Catalog) -> tuple[AudienceDefinition, list[Issue]]:
    """Normalize every condition and run cross-condition checks.

    Conditions that fail (unknown field, policy, bad value) stay in the returned definition so the
    user sees them and decides; any error makes the status invalid, and invalid definitions are
    never sized or approved. Cross-condition checks run on the valid conditions only.
    """
    issues: list[Issue] = []
    kept: list[Condition] = []
    valid: list[Condition] = []
    seen: set[tuple[str, str, str]] = set()
    for cond in definition.conditions:
        normalized, problems = normalize(cond, catalog, definition.channel)
        candidate = normalized or cond
        key = (candidate.attribute, candidate.op, repr(candidate.value))
        if key in seen:
            continue
        seen.add(key)
        issues += problems
        kept.append(candidate)
        if normalized is not None:
            valid.append(normalized)

    valid_only = definition.model_copy(update={"conditions": valid})
    issues += _check_scope(valid_only, catalog)
    issues += _check_contradictions(valid_only, catalog)
    if not kept and not any(i.level == "error" for i in issues):
        issues.append(Issue(level="warning", code="no_conditions", message="No conditions: this would target every consented line."))
    return definition.model_copy(update={"conditions": kept}), issues


def _coerce_value(attr: Attribute, op: str, value: Any) -> Any:
    if op in {"in", "not_in"}:
        items = value if isinstance(value, list) else [value]
        if not items:
            raise ValueError("needs at least one value")
        if len(items) > max(len(attr.values), MAX_LIST_VALUES):
            raise ValueError(f"too many values ({len(items)})")
        return [_coerce_scalar(attr, v) for v in items]
    if op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("'between' needs exactly two values [low, high]")
        low, high = (_coerce_scalar(attr, v) for v in value)
        if low > high:
            raise ValueError(f"low bound {low} is above high bound {high}")
        return [low, high]
    return _coerce_scalar(attr, value)


def _coerce_scalar(attr: Attribute, value: Any) -> Any:
    if attr.type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise ValueError(f"expected true/false, got {value!r}")
    if attr.type == "enum":
        if value not in attr.values:
            raise ValueError(f"'{value}' is not one of {', '.join(attr.values)}")
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"expected a number, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"expected a finite number, got {value!r}")
    if attr.type == "integer":
        if number != int(number):
            raise ValueError(f"expected a whole number, got {value!r}")
        number = int(number)
    if (attr.min is not None and number < attr.min) or (attr.max is not None and number > attr.max):
        raise ValueError(f"{value} is outside the valid range {attr.min:g}..{attr.max:g} {attr.unit}".rstrip())
    return number


def _check_scope(definition: AudienceDefinition, catalog: Catalog) -> list[Issue]:
    """Fields like days_since_last_recharge only exist for some lines; using them narrows the audience."""
    issues = []
    for cond in definition.conditions:
        attr = catalog.attributes[cond.attribute]
        for scope_attr, scope_value in attr.applies_to.items():
            allowed = _allowed_values(definition, scope_attr, catalog)
            if scope_value not in allowed:
                issues.append(_err(
                    "out_of_scope_condition",
                    f"{attr.label} only exists for {scope_attr}={scope_value}, but the audience excludes those lines.",
                    attr.name,
                ))
            elif allowed != {scope_value}:
                issues.append(Issue(
                    level="warning", code="implicit_scope", attribute=attr.name,
                    message=f"{attr.label} only exists for {scope_attr}={scope_value}; other lines will be left out.",
                ))
    return issues


def _allowed_values(definition: AudienceDefinition, attribute: str, catalog: Catalog) -> set[str]:
    allowed = set(catalog.attributes[attribute].values)
    for cond in definition.conditions:
        if cond.attribute != attribute:
            continue
        if cond.op == "eq":
            allowed &= {cond.value}
        elif cond.op == "in":
            allowed &= set(cond.value)
        elif cond.op == "neq":
            allowed -= {cond.value}
        elif cond.op == "not_in":
            allowed -= set(cond.value)
    return allowed


def _check_contradictions(definition: AudienceDefinition, catalog: Catalog) -> list[Issue]:
    issues = []
    for name in {c.attribute for c in definition.conditions}:
        attr = catalog.attributes[name]
        conds = [c for c in definition.conditions if c.attribute == name]
        if attr.type == "enum" and not _allowed_values(definition, name, catalog):
            issues.append(_err("contradictory_conditions", f"No value of {attr.label} satisfies all conditions.", name))
        elif attr.type == "boolean" and len({c.value for c in conds}) > 1:
            issues.append(_err("contradictory_conditions", f"{attr.label} is required to be both true and false.", name))
        elif attr.type in {"integer", "number"} and _numeric_range_empty(conds, attr.type == "integer"):
            issues.append(_err("contradictory_conditions", f"No value of {attr.label} satisfies all conditions.", name))
    return issues


def _numeric_range_empty(conds: list[Condition], integer: bool) -> bool:
    """Intersect every bound; True if no value satisfies all of them."""
    low, high = -math.inf, math.inf
    low_open = high_open = False

    def tighten_low(value: float, is_open: bool) -> None:
        nonlocal low, low_open
        if value > low or (value == low and is_open):
            low, low_open = value, is_open or (value == low and low_open)

    def tighten_high(value: float, is_open: bool) -> None:
        nonlocal high, high_open
        if value < high or (value == high and is_open):
            high, high_open = value, is_open or (value == high and high_open)

    for c in conds:
        if c.op in {"gt", "gte"}:
            tighten_low(c.value, c.op == "gt")
        elif c.op in {"lt", "lte"}:
            tighten_high(c.value, c.op == "lt")
        elif c.op == "eq":
            tighten_low(c.value, False)
            tighten_high(c.value, False)
        elif c.op == "between":
            tighten_low(c.value[0], False)
            tighten_high(c.value[1], False)
    if integer:
        if low_open:
            low, low_open = math.floor(low) + 1, False
        if high_open:
            high, high_open = math.ceil(high) - 1, False
    if low > high:
        return True
    return low == high and (low_open or high_open)


def _err(code: str, message: str, attribute: str | None = None) -> Issue:
    return Issue(level="error", code=code, message=message, attribute=attribute)
