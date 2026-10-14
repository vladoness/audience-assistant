"""Decision support: choices show their consequences before they are made, and every approval says
how it will be judged (a goal and a holdout group)."""

import json
from dataclasses import replace

from app.lint import lint
from app.models import AudienceDefinition, Condition
from tests.conftest import ScriptedProvider, proposal

ACTIVE_COAST = proposal(conditions=[{"attribute": "region", "op": "eq", "value": "Coast"}], terms=["active"])
CHURN_CAPITAL = proposal(conditions=[{"attribute": "region", "op": "eq", "value": "Capital"}], terms=["churn_risk"])
COAST = AudienceDefinition(conditions=[Condition(attribute="region", op="eq", value="Coast")])


def with_option(term, option_id, base=COAST):
    option = term.option(option_id)
    return base.model_copy(update={"conditions": [*base.conditions, *(Condition(**c) for c in option.conditions)]})


def approve(client, r, definition=None, **extra):
    return client.post("/api/approve", json={"request_id": r["request_id"], "name": "Test audience", "approver": "Ana",
                                             "goal": "Win back lapsed users", "definition": definition or r["definition"],
                                             **extra})


def test_compare_matches_set_arithmetic(engine, catalog):
    term = catalog.terms["active"]
    usage, billing = with_option(term, "active_usage"), with_option(term, "active_billing")
    a, b = engine.profile_ids(usage), engine.profile_ids(billing)
    assert engine.compare([usage, billing]) == [(len(a), len(a - b)), (len(b), len(b - a))]


def test_each_answer_shows_what_it_would_reach_before_it_is_chosen(make_client, engine, catalog):
    client = make_client(ScriptedProvider(ACTIVE_COAST))
    clarification = client.post("/api/interpret", json={"request": "active customers in the Coast"}).json()["clarifications"][0]
    term = catalog.terms["active"]
    expected = engine.compare([with_option(term, "active_usage"), with_option(term, "active_billing")])
    assert [(o["reachable"], o["only_here"]) for o in clarification["options"]] == expected
    assert [o["working_default"] for o in clarification["options"]] == [True, False]
    assert "D-030" in clarification["working_default_note"] and "Marketing Operations" in clarification["working_default_note"]


def test_a_term_without_a_working_default_shows_none(make_client):
    client = make_client(ScriptedProvider(CHURN_CAPITAL))
    clarification = client.post("/api/interpret", json={"request": "likely to churn in the Capital"}).json()["clarifications"][0]
    assert not any(o["working_default"] for o in clarification["options"]) and clarification["working_default_note"] is None
    assert all(o["reachable"] is not None for o in clarification["options"])


def test_no_consequences_are_shown_for_an_invalid_definition(make_client):
    blocked = proposal(conditions=[{"attribute": "credit_risk_band", "op": "eq", "value": "high"}], terms=["active"])
    client = make_client(ScriptedProvider(blocked))
    r = client.post("/api/interpret", json={"request": "active customers with high credit risk"}).json()
    assert r["status"] == "invalid"
    assert all(o["reachable"] is None for o in r["clarifications"][0]["options"])


def test_approval_needs_a_goal(make_client):
    client = make_client(ScriptedProvider(proposal(conditions=[{"attribute": "region", "op": "eq", "value": "Coast"}])))
    r = client.post("/api/interpret", json={"request": "customers in the Coast"}).json()
    body = {"request_id": r["request_id"], "name": "Coast", "approver": "Ana", "definition": r["definition"]}
    assert client.post("/api/approve", json=body).status_code == 422


def test_every_approval_records_a_holdout_and_how_success_is_measured(make_client, engine, catalog):
    client = make_client(ScriptedProvider(proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "prepaid"}])))
    r = client.post("/api/interpret", json={"request": "prepaid customers"}).json()
    response = approve(client, r).json()
    m = response["measurement"]
    assert m["goal"] == "Win back lapsed users" and m["parameters"] == "measurement v1"
    assert m["target_lines"] + m["holdout_lines"] == response["reachable"]
    assert abs(m["holdout_lines"] / response["reachable"] - m["holdout_share"]) < 0.02
    prepaid = AudienceDefinition(conditions=[Condition(attribute="line_type", op="eq", value="prepaid")])
    assert (m["target_lines"], m["holdout_lines"]) == engine.split(prepaid, r["request_id"], m["holdout_share"])
    assert m["measurable"] is True and "held-out" in m["success_measure"]
    assert json.loads(client.get("/api/audiences").json()[0]["measurement_json"]) == m


def test_a_holdout_too_small_to_measure_is_flagged(make_client):
    narrow = proposal(conditions=[{"attribute": "region", "op": "eq", "value": "North"},
                                  {"attribute": "device_os", "op": "eq", "value": "feature_phone"},
                                  {"attribute": "plan_tier", "op": "eq", "value": "premium"}])
    client = make_client(ScriptedProvider(narrow))
    r = client.post("/api/interpret", json={"request": "premium feature phones in the North"}).json()
    m = approve(client, r).json()["measurement"]
    assert m["measurable"] is False and "too small" in m["note"]


def test_lint_checks_working_defaults(catalog):
    active = catalog.terms["active"]
    broken = replace(catalog, terms={**catalog.terms, "active": replace(active, working_default={"option": "nope"})})
    errors = " ".join(lint(broken).errors)
    assert "is not one of its options" in errors and "who adopted it and why" in errors
