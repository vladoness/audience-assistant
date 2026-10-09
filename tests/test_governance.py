import shutil

import yaml

from app.catalog import load_catalog
from app.governance import impact, reexpand, snapshot
from app.lint import lint
from app.models import AudienceDefinition, Condition
from app.settings import ROOT


def config_copy(tmp_path, edit=None):
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    if edit:
        edit(target)
    return target


def set_yaml(path, mutate):
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_shipped_governance_config_lints_clean(catalog):
    report = lint(catalog)
    assert report.receipt == "governance lint: 0 errors, 0 warnings"


def test_lint_catches_broken_references_and_vague_rules(tmp_path):
    def edit(cfg):
        set_yaml(cfg / "glossary.yaml", lambda d: d["terms"]["high_value"]["conditions"][0].update(
            value={"param": "high_value.missing_key"}))
        set_yaml(cfg / "glossary.yaml", lambda d: d["terms"]["app_user"].update(
            rule="A line is an app user. Unless it is a test line."))
        set_yaml(cfg / "catalog.yaml", lambda d: d["attributes"]["roaming_30d"].update(labels=["NOT_A_LABEL"]))

    report = lint(load_catalog(config_copy(tmp_path, edit)))
    assert any("high_value.missing_key" in e for e in report.errors)
    assert any("unknown label NOT_A_LABEL" in e for e in report.errors)
    assert any("app_user: rule is more than one sentence" in w for w in report.warnings)


def test_rule_thresholds_come_from_the_versioned_parameter_set(catalog):
    [condition] = catalog.terms["high_value"].conditions
    assert condition["value"] == 40
    assert catalog.terms["high_value"].parameter_sets == ("high_value",)


def test_parameter_change_flags_approved_audiences_and_reexpands_them(tmp_path, catalog):
    definition = AudienceDefinition(conditions=[
        Condition(attribute="line_type", op="eq", value="postpaid"),
        Condition(attribute="arpu_3m", op="gte", value=40.0, source="glossary:high_value"),
    ])
    snap = snapshot(definition, catalog)
    assert snap["high_value"]["decision"] == "D-014"
    assert snap["high_value"]["parameters"] == {"high_value": 3}
    assert impact(snap, catalog) == []

    def raise_threshold(cfg):
        set_yaml(cfg / "parameters.yaml", lambda d: d["parameter_sets"]["high_value"].update(
            version=4, values={"min_arpu_usd": 45}))

    changed = load_catalog(config_copy(tmp_path, raise_threshold))
    assert impact(snap, changed) == ["high_value: parameters high_value v3 → v4"]
    current = reexpand(definition, changed)
    assert {"attribute": "arpu_3m", "op": "gte", "value": 45} in [
        c.model_dump(include={"attribute", "op", "value"}) for c in current.conditions]
    assert {"attribute": "line_type", "op": "eq", "value": "postpaid"} in [
        c.model_dump(include={"attribute", "op", "value"}) for c in current.conditions]


def test_interim_choice_is_flagged_once_the_term_is_decided(tmp_path, catalog):
    definition = AudienceDefinition(conditions=[
        Condition(attribute="usage_events_30d", op="gte", value=1, source="glossary:active=active_usage")])
    snap = snapshot(definition, catalog)

    def decide_active(cfg):
        def mutate(d):
            d["terms"]["active"] = {
                "status": "decided", "aliases": ["active"], "rule": "A line is active when it used the network in the last 30 days.",
                "decision": {"id": "D-030", "owner": "Marketing Analytics", "date": "2026-10-20"},
                "conditions": [{"attribute": "usage_events_30d", "op": "gte", "value": 1}],
            }
        set_yaml(cfg / "glossary.yaml", mutate)

    changed = load_catalog(config_copy(tmp_path, decide_active))
    assert impact(snap, changed) == ["active: now decided (D-030); this audience used an interim choice"]
