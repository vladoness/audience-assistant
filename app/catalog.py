"""Loads the governance configuration (config/*.yaml) into one immutable Catalog.

    catalog.yaml     targetable fields, types, ranges, and data-usage labels per field
    policies.yaml    labels, marketing actions (one per channel, with its consent field) and
                     the policies that deny labels for actions
    parameters.yaml  versioned parameter sets (thresholds) with owner and effective date
    glossary.yaml    governed business terms: one owned rule sentence per decided term, its
                     conditions referencing parameters; open terms list competing options

All four files are versioned and stamped on every interpretation and approval, so any audience
can be traced to the definitions, policies and parameter versions in force when it was approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Attribute:
    name: str
    label: str
    type: str  # enum | integer | number | boolean
    description: str = ""
    values: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    unit: str = ""
    labels: tuple[str, ...] = ()
    applies_to: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketingAction:
    name: str
    channel: str
    consent_attribute: str


@dataclass(frozen=True)
class Policy:
    id: str
    name: str
    status: str
    deny_labels: tuple[str, ...]
    marketing_actions: tuple[str, ...]


@dataclass(frozen=True)
class ParameterSet:
    name: str
    version: int
    effective: str
    owner: str
    approved_by: str
    values: dict[str, float]


@dataclass(frozen=True)
class TermOption:
    id: str
    label: str
    conditions: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Term:
    name: str
    status: str  # decided | open
    aliases: tuple[str, ...]
    rule: str = ""
    decision: dict[str, Any] = field(default_factory=dict)
    conditions: tuple[dict[str, Any], ...] = ()
    parameter_sets: tuple[str, ...] = ()
    question: str = ""
    owner_pending: str = ""
    options: tuple[TermOption, ...] = ()

    def option(self, option_id: str) -> TermOption | None:
        return next((o for o in self.options if o.id == option_id), None)


@dataclass(frozen=True)
class Catalog:
    versions: dict[str, str]
    grain: str
    attributes: dict[str, Attribute]
    terms: dict[str, Term]
    labels: dict[str, str]
    marketing_actions: dict[str, MarketingAction]
    policies: tuple[Policy, ...]
    parameter_sets: dict[str, ParameterSet]
    default_channel: str
    unresolved: tuple[str, ...] = ()  # config references that failed to resolve; reported by lint

    @property
    def version(self) -> str:
        return self.versions["catalog"]

    @property
    def glossary_version(self) -> str:
        return self.versions["glossary"]

    @property
    def fingerprint(self) -> str:
        """One string that changes whenever any governance file changes (used in cache keys)."""
        return "|".join(f"{k}={v}" for k, v in sorted(self.versions.items()))

    @property
    def channels(self) -> dict[str, str]:
        return {a.channel: a.consent_attribute for a in self.marketing_actions.values()}

    def action_for(self, channel: str) -> MarketingAction:
        return next(a for a in self.marketing_actions.values() if a.channel == channel)

    def violations(self, attribute: str, channel: str) -> list[Policy]:
        """Enabled policies that forbid using this field for this channel's marketing action."""
        labels = set(self.attributes[attribute].labels)
        action = self.action_for(channel).name
        return [p for p in self.policies
                if p.status == "enabled" and action in p.marketing_actions and labels & set(p.deny_labels)]

    def targetable_somewhere(self, attribute: str) -> bool:
        return any(not self.violations(attribute, a.channel) for a in self.marketing_actions.values())

    def prompt_view(self) -> str:
        """Compact, model-readable rendering of everything the model is allowed to use."""
        lines = [f"Grain: {self.grain}.", "", "ATTRIBUTES (attribute | type | allowed values or range | meaning):"]
        for a in self.attributes.values():
            blocked = {ch: self.violations(a.name, ch) for ch in self.channels}
            if all(blocked.values()):
                policy = blocked[self.default_channel][0]
                lines.append(f"- {a.name} | NOT TARGETABLE | policy {policy.id}: {policy.name}")
                continue
            if a.type == "enum":
                allowed = ", ".join(a.values)
            elif a.type == "boolean":
                allowed = "true/false"
            else:
                allowed = f"{a.min:g}..{a.max:g} {a.unit}".strip()
            notes = []
            if a.applies_to:
                notes.append(f"Only exists for {', '.join(f'{k}={v}' for k, v in a.applies_to.items())}.")
            for channel, policies in blocked.items():
                if policies:
                    notes.append(f"Not usable for {channel} campaigns (policy {policies[0].id}).")
            lines.append(f"- {a.name} | {a.type} | {allowed} | {a.description} {' '.join(notes)}".rstrip())
        lines += ["", "GLOSSARY TERMS (term | status | meaning):"]
        for t in self.terms.values():
            meaning = t.rule if t.status == "decided" else "OPEN: competing definitions, the user must choose"
            lines.append(f"- {t.name} | {t.status} | {meaning} | phrases: {', '.join(t.aliases)}")
        lines += ["", f"CHANNELS: {', '.join(self.channels)} (default {self.default_channel})."]
        return "\n".join(lines)


def load_catalog(config_dir: Path) -> Catalog:
    cat = _yaml(config_dir / "catalog.yaml")
    pol = _yaml(config_dir / "policies.yaml")
    par = _yaml(config_dir / "parameters.yaml")
    glo = _yaml(config_dir / "glossary.yaml")

    attributes = {
        name: Attribute(
            name=name, label=spec["label"], type=spec["type"], description=spec.get("description", ""),
            values=tuple(spec.get("values", ())), min=spec.get("min"), max=spec.get("max"),
            unit=spec.get("unit", ""), labels=tuple(spec.get("labels", ())), applies_to=spec.get("applies_to", {}),
        )
        for name, spec in cat["attributes"].items()
    }
    actions = {
        name: MarketingAction(name=name, channel=spec["channel"], consent_attribute=spec["consent_attribute"])
        for name, spec in pol["marketing_actions"].items()
    }
    policies = tuple(
        Policy(id=p["id"], name=p["name"], status=p.get("status", "enabled"),
               deny_labels=tuple(p["deny_labels"]), marketing_actions=tuple(p["marketing_actions"]))
        for p in pol["policies"]
    )
    parameter_sets = {
        name: ParameterSet(name=name, version=int(spec["version"]), effective=str(spec["effective"]),
                           owner=spec["owner"], approved_by=spec.get("approved_by", ""), values=spec["values"])
        for name, spec in par["parameter_sets"].items()
    }

    unresolved: list[str] = []

    def resolve(conditions: list[dict[str, Any]], where: str) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
        out, used = [], []
        for c in conditions:
            value = c["value"]
            if isinstance(value, dict) and "param" in value:
                set_name, _, key = value["param"].partition(".")
                pset = parameter_sets.get(set_name)
                if pset is None or key not in pset.values:
                    unresolved.append(f"{where}: unknown parameter '{value['param']}'")
                    continue
                value = pset.values[key]
                used.append(set_name)
            out.append({**c, "value": value})
        return tuple(out), tuple(dict.fromkeys(used))

    terms = {}
    for name, spec in glo["terms"].items():
        conditions, used = resolve(spec.get("conditions", []), f"term {name}")
        options = []
        for o in spec.get("options", ()):
            opt_conditions, opt_used = resolve(o["conditions"], f"term {name} option {o['id']}")
            options.append(TermOption(id=o["id"], label=o["label"], conditions=opt_conditions))
            used += opt_used
        terms[name] = Term(
            name=name, status=spec["status"], aliases=tuple(spec.get("aliases", ())), rule=spec.get("rule", ""),
            decision=spec.get("decision", {}), conditions=conditions, parameter_sets=tuple(dict.fromkeys(used)),
            question=spec.get("question", ""), owner_pending=spec.get("owner_pending", ""), options=tuple(options),
        )

    default_action = actions[pol["default_marketing_action"]]
    return Catalog(
        versions={"catalog": cat["version"], "glossary": glo["version"], "policies": pol["version"],
                  "parameters": par["version"]},
        grain=cat["grain"],
        attributes=attributes,
        terms=terms,
        labels=pol["labels"],
        marketing_actions=actions,
        policies=policies,
        parameter_sets=parameter_sets,
        default_channel=default_action.channel,
        unresolved=tuple(unresolved),
    )


def _yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())
