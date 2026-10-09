"""Mechanical validation of the governance configuration, run at startup and by `make lint`.

Errors stop the service (a glossary term pointing at a field that doesn't exist would make every
audience using it wrong). Warnings are the governance backlog: open terms with no owner,
rules that need a second sentence, parameters with no approver. The output is a receipt:
"governance lint: 0 errors, 2 warnings".
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

from app.catalog import Catalog, load_catalog
from app.settings import ROOT


@dataclass
class LintReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def receipt(self) -> str:
        return f"governance lint: {len(self.errors)} errors, {len(self.warnings)} warnings"


def lint(catalog: Catalog) -> LintReport:
    report = LintReport(errors=list(catalog.unresolved))
    err, warn = report.errors.append, report.warnings.append

    for a in catalog.attributes.values():
        for label in a.labels:
            if label not in catalog.labels:
                err(f"field {a.name}: unknown label {label}")
        if not a.description:
            warn(f"field {a.name}: no description (the model maps requests using it)")
        for scope_attr in a.applies_to:
            if scope_attr not in catalog.attributes:
                err(f"field {a.name}: applies_to references unknown field {scope_attr}")

    for action in catalog.marketing_actions.values():
        consent = catalog.attributes.get(action.consent_attribute)
        if consent is None:
            err(f"marketing action {action.name}: consent field {action.consent_attribute} is not in the catalog")
        elif not catalog.violations(consent.name, action.channel):
            err(f"marketing action {action.name}: consent field {consent.name} is targetable; it must be policy-blocked")

    for p in catalog.policies:
        for label in p.deny_labels:
            if label not in catalog.labels:
                err(f"policy {p.id}: unknown label {label}")
        for action in p.marketing_actions:
            if action not in catalog.marketing_actions:
                err(f"policy {p.id}: unknown marketing action {action}")

    for ps in catalog.parameter_sets.values():
        if not ps.approved_by:
            warn(f"parameter set {ps.name} v{ps.version}: no approver recorded")

    for t in catalog.terms.values():
        condition_sets = [t.conditions] + [o.conditions for o in t.options]
        for conditions in condition_sets:
            for c in conditions:
                if c["attribute"] not in catalog.attributes:
                    err(f"term {t.name}: unknown field {c['attribute']}")
        if t.status == "decided":
            if not t.conditions:
                err(f"term {t.name}: decided but has no conditions")
            if not t.decision.get("id") or not t.decision.get("owner"):
                err(f"term {t.name}: decided without a decision id and owner")
            if not t.rule:
                err(f"term {t.name}: decided without a rule sentence")
            elif len(re.findall(r"[.!?](\s|$)", t.rule.strip())) > 1:
                warn(f"term {t.name}: rule is more than one sentence; split out parameters or exceptions")
        elif t.status == "open":
            if len(t.options) < 2:
                err(f"term {t.name}: open terms need at least two options to choose from")
            if not t.owner_pending:
                warn(f"term {t.name}: open with no owner named to decide it")
        else:
            err(f"term {t.name}: unknown status {t.status}")

    aliases: dict[str, str] = {}
    for t in catalog.terms.values():
        for alias in t.aliases:
            if alias.lower() in aliases and aliases[alias.lower()] != t.name:
                warn(f"alias '{alias}' is used by both {aliases[alias.lower()]} and {t.name}")
            aliases[alias.lower()] = t.name
    return report


def main() -> None:
    report = lint(load_catalog(ROOT / "config"))
    for e in report.errors:
        print(f"ERROR    {e}")
    for w in report.warnings:
        print(f"WARNING  {w}")
    print(report.receipt)
    sys.exit(1 if report.errors else 0)


if __name__ == "__main__":
    main()
