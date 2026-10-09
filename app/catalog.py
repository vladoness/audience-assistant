"""Loads the governed attribute catalog and business glossary (config/*.yaml).

Both files are versioned; their versions are stamped on every interpretation and approved
audience so an approval can always be traced back to the definitions in force at the time.
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
    usage: str = "targetable"  # targetable | restricted | system
    restriction: str = ""
    applies_to: dict[str, str] = field(default_factory=dict)

    @property
    def targetable(self) -> bool:
        return self.usage == "targetable"


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
    definition: str = ""
    decision: dict[str, Any] = field(default_factory=dict)
    conditions: tuple[dict[str, Any], ...] = ()
    question: str = ""
    owner_pending: str = ""
    options: tuple[TermOption, ...] = ()

    def option(self, option_id: str) -> TermOption | None:
        return next((o for o in self.options if o.id == option_id), None)


@dataclass(frozen=True)
class Catalog:
    version: str
    glossary_version: str
    grain: str
    attributes: dict[str, Attribute]
    terms: dict[str, Term]
    channels: dict[str, str]
    default_channel: str

    def prompt_view(self) -> str:
        """Compact, model-readable rendering of everything the model is allowed to use."""
        lines = [f"Grain: {self.grain}.", "", "ATTRIBUTES (attribute | type | allowed values or range | meaning):"]
        for a in self.attributes.values():
            if a.usage != "targetable":
                lines.append(f"- {a.name} | NOT TARGETABLE ({a.usage}) | {a.restriction}")
                continue
            if a.type == "enum":
                allowed = ", ".join(a.values)
            elif a.type == "boolean":
                allowed = "true/false"
            else:
                allowed = f"{a.min:g}..{a.max:g} {a.unit}".strip()
            scope = f" Only exists for {', '.join(f'{k}={v}' for k, v in a.applies_to.items())}." if a.applies_to else ""
            lines.append(f"- {a.name} | {a.type} | {allowed} | {a.description}{scope}")
        lines += ["", "GLOSSARY TERMS (term | status | meaning):"]
        for t in self.terms.values():
            meaning = t.definition if t.status == "decided" else "OPEN: competing definitions, the user must choose"
            lines.append(f"- {t.name} | {t.status} | {meaning} | phrases: {', '.join(t.aliases)}")
        lines += ["", f"CHANNELS: {', '.join(self.channels)} (default {self.default_channel})."]
        return "\n".join(lines)


def load_catalog(config_dir: Path) -> Catalog:
    cat = yaml.safe_load((config_dir / "catalog.yaml").read_text())
    glo = yaml.safe_load((config_dir / "glossary.yaml").read_text())

    attributes = {}
    for name, spec in cat["attributes"].items():
        attributes[name] = Attribute(
            name=name,
            label=spec["label"],
            type=spec["type"],
            description=spec.get("description", ""),
            values=tuple(spec.get("values", ())),
            min=spec.get("min"),
            max=spec.get("max"),
            unit=spec.get("unit", ""),
            usage=spec.get("usage", "targetable"),
            restriction=spec.get("restriction", ""),
            applies_to=spec.get("applies_to", {}),
        )

    terms = {}
    for name, spec in glo["terms"].items():
        terms[name] = Term(
            name=name,
            status=spec["status"],
            aliases=tuple(spec.get("aliases", ())),
            definition=spec.get("definition", ""),
            decision=spec.get("decision", {}),
            conditions=tuple(spec.get("conditions", ())),
            question=spec.get("question", ""),
            owner_pending=spec.get("owner_pending", ""),
            options=tuple(
                TermOption(id=o["id"], label=o["label"], conditions=tuple(o["conditions"]))
                for o in spec.get("options", ())
            ),
        )

    catalog = Catalog(
        version=cat["version"],
        glossary_version=glo["version"],
        grain=cat["grain"],
        attributes=attributes,
        terms=terms,
        channels=cat["channels"],
        default_channel=cat["default_channel"],
    )
    _check_consistency(catalog)
    return catalog


def _check_consistency(catalog: Catalog) -> None:
    """Fail at startup if the glossary references attributes the catalog doesn't define."""
    for term in catalog.terms.values():
        condition_sets = [term.conditions] + [o.conditions for o in term.options]
        for conditions in condition_sets:
            for c in conditions:
                if c["attribute"] not in catalog.attributes:
                    raise ValueError(f"Glossary term '{term.name}' uses unknown attribute '{c['attribute']}'")
    for channel, column in catalog.channels.items():
        if column not in catalog.attributes:
            raise ValueError(f"Channel '{channel}' points to unknown consent attribute '{column}'")
