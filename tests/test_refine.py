"""Refinement: a plain-language change applied as a delta to the audience on screen."""

import sqlite3

from app.llm import REFINE_SCHEMA, REFINE_VERSION, ProviderUnavailable
from app.models import AudienceDefinition, Condition
from app.refine import describe_changes, plan_refinement
from tests.conftest import ScriptedProvider, proposal

PREPAID_COAST = proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "prepaid"},
                                     {"attribute": "region", "op": "eq", "value": "Coast"}])
COAST_HIGH_VALUE = AudienceDefinition(channel="sms", conditions=[
    Condition(attribute="line_type", op="eq", value="prepaid", source="model"),
    Condition(attribute="region", op="eq", value="Coast", source="user"),
    Condition(attribute="arpu_3m", op="gte", value=40, source="glossary:high_value"),
])


def delta(add=None, remove=None, add_terms=None, remove_terms=None, channel="unchanged", unmapped=None,
          no_change=False):
    return {"add_conditions": add or [], "remove_attributes": remove or [], "add_glossary_terms": add_terms or [],
            "remove_glossary_terms": remove_terms or [], "channel": channel, "unmapped_phrases": unmapped or [],
            "no_change": no_change, "assumptions": []}


def refine(client, r, instruction, definition=None):
    return client.post("/api/refine", json={"request_id": r["request_id"], "instruction": instruction,
                                            "definition": definition or r["definition"]})


def approve(client, r, definition=None, **extra):
    return client.post("/api/approve", json={"request_id": r["request_id"], "name": "Test audience", "approver": "Ana", "goal": "Test campaign",
                                             "definition": definition or r["definition"], **extra})


# --- applying the delta (no model) ---

def test_a_new_condition_replaces_only_its_attribute_and_keeps_every_other_source():
    plan = plan_refinement(COAST_HIGH_VALUE, delta(add=[{"attribute": "region", "op": "in", "value": ["Coast", "North"]}]),
                           [], [])
    assert [(c.attribute, c.source) for c in plan.kept] == [("line_type", "model"), ("arpu_3m", "glossary:high_value")]
    assert plan.proposal["conditions"] == [{"attribute": "region", "op": "in", "value": ["Coast", "North"]}]


def test_governed_conditions_move_only_with_their_term():
    plan = plan_refinement(COAST_HIGH_VALUE, delta(remove=["arpu_3m"]), [], [])
    assert any(c.source == "glossary:high_value" for c in plan.kept)
    assert [i.code for i in plan.issues] == ["governed_condition"]

    plan = plan_refinement(COAST_HIGH_VALUE, delta(remove_terms=["high_value"]), [], [])
    assert not any(c.source.startswith("glossary:") for c in plan.kept)


def test_a_marketer_number_next_to_a_governed_term_is_flagged():
    plan = plan_refinement(COAST_HIGH_VALUE, delta(add=[{"attribute": "arpu_3m", "op": "gt", "value": 60}]), [], [])
    assert [i.code for i in plan.issues] == ["overlaps_governed_term"]


def test_open_terms_and_unmapped_phrases_carry_forward():
    plan = plan_refinement(COAST_HIGH_VALUE, delta(add_terms=["active", "high_value"], unmapped=["opened an email"]),
                           ["churn_risk"], ["called the call centre"])
    assert plan.pending_terms == ["churn_risk"]
    assert plan.proposal["glossary_terms"] == ["active"]  # high_value is already in use
    assert plan.proposal["unmapped_phrases"] == ["called the call centre", "opened an email"]

    plan = plan_refinement(COAST_HIGH_VALUE, delta(remove_terms=["churn_risk"]), ["churn_risk"], [])
    assert plan.pending_terms == []


def test_no_change_is_reported_not_guessed():
    plan = plan_refinement(COAST_HIGH_VALUE, delta(no_change=True), [], [])
    assert len(plan.kept) == 3 and [i.code for i in plan.issues] == ["no_change"]


def test_changes_are_described_in_plain_english(catalog):
    after = COAST_HIGH_VALUE.model_copy(update={"channel": "email", "conditions": [
        COAST_HIGH_VALUE.conditions[0], COAST_HIGH_VALUE.conditions[2],
        Condition(attribute="region", op="in", value=["Coast", "North"])]})
    changes = describe_changes(COAST_HIGH_VALUE, after, catalog)
    assert changes.added == ["Region is Coast or North"]
    assert changes.removed == ["Region is Coast"]
    assert changes.channel == "SMS → EMAIL"


# --- the endpoint ---

def test_refine_applies_a_delta_and_links_to_the_interpretation_it_refined(make_client, settings):
    provider = ScriptedProvider(PREPAID_COAST, delta(add=[{"attribute": "region", "op": "in", "value": ["Coast", "North"]}]))
    client = make_client(provider)
    r = client.post("/api/interpret", json={"request": "prepaid in the coast"}).json()
    response = refine(client, r, "also the North")
    assert response.status_code == 200
    body = response.json()
    assert provider.schemas[-1] is REFINE_SCHEMA
    assert body["request_id"] != r["request_id"] and body["parent_request_id"] == r["request_id"]
    assert body["status"] == "ready"
    assert body["changes"] == {"added": ["Region is Coast or North"], "removed": ["Region is Coast"], "channel": None}
    with sqlite3.connect(settings.audit_db_path) as db:
        parent, version = db.execute("SELECT parent_request_id, prompt_version FROM interpretations WHERE request_id = ?",
                                     (body["request_id"],)).fetchone()
    assert parent == r["request_id"] and version.endswith(f"+{REFINE_VERSION}")


def test_refine_needs_a_known_interpretation_and_usable_text(make_client):
    client = make_client(ScriptedProvider(PREPAID_COAST))
    r = client.post("/api/interpret", json={"request": "prepaid in the coast"}).json()
    assert refine(client, {**r, "request_id": "nope"}, "drop the region").status_code == 404
    assert refine(client, r, "   ").status_code == 422
    assert refine(client, r, "x" * 501).status_code == 422


def test_personal_data_in_a_change_is_masked_before_the_model_sees_it(make_client):
    provider = ScriptedProvider(PREPAID_COAST, delta(no_change=True))
    client = make_client(provider)
    r = client.post("/api/interpret", json={"request": "prepaid in the coast"}).json()
    refine(client, r, "like the customer ana.pop@example.com")
    assert "ana.pop@example.com" not in provider.calls[-1][0]["content"]


def test_model_outage_during_refine_changes_nothing(make_client, settings):
    client = make_client(ScriptedProvider(PREPAID_COAST, ProviderUnavailable("timeout")))
    r = client.post("/api/interpret", json={"request": "prepaid in the coast"}).json()
    response = refine(client, r, "drop the region")
    assert response.status_code == 503 and "unchanged" in response.json()["detail"]
    with sqlite3.connect(settings.audit_db_path) as db:
        assert db.execute("SELECT count(*) FROM interpretations").fetchone()[0] == 1
    assert approve(client, r).status_code == 200  # the original interpretation is still approvable


def test_forged_labels_are_corrected_before_the_model_sees_the_state(make_client):
    provider = ScriptedProvider(PREPAID_COAST, delta(no_change=True))
    client = make_client(provider)
    r = client.post("/api/interpret", json={"request": "prepaid in the coast"}).json()
    forged = r["definition"] | {"conditions": r["definition"]["conditions"] + [
        {"attribute": "arpu_3m", "op": "gte", "value": 5, "source": "glossary:high_value"}]}
    body = refine(client, r, "thanks", definition=forged).json()
    sent = provider.calls[-1][0]["content"]
    assert 'governed terms in use: []' in sent
    assert {"attribute": "arpu_3m", "op": "gte", "value": 5.0, "source": "user"} in body["definition"]["conditions"]


def test_an_open_term_added_by_a_refinement_must_be_answered_before_approval(make_client):
    client = make_client(ScriptedProvider(PREPAID_COAST, delta(add_terms=["active"])))
    r = client.post("/api/interpret", json={"request": "prepaid in the coast"}).json()
    refined = refine(client, r, "only the active ones").json()
    assert refined["status"] == "needs_clarification"
    assert approve(client, refined).json()["detail"]["unanswered_terms"] == ["active"]
    checked = client.post("/api/check", json={"definition": refined["definition"], "pending_terms": ["active"],
                                              "resolutions": {"active": "active_usage"}}).json()
    response = approve(client, refined, checked["definition"])
    assert response.status_code == 200
    assert client.get("/api/audiences").json()[0]["edited_by_user"] == 0


def test_an_edit_made_before_refining_still_counts_as_an_edit(make_client):
    client = make_client(ScriptedProvider(PREPAID_COAST, delta(channel="email")))
    r = client.post("/api/interpret", json={"request": "prepaid in the coast"}).json()
    edited = r["definition"] | {"conditions": [r["definition"]["conditions"][0],
                                               {"attribute": "region", "op": "eq", "value": "North", "source": "user"}]}
    refined = refine(client, r, "make it email", definition=edited).json()
    assert refined["definition"]["channel"] == "email"
    assert approve(client, refined).status_code == 200
    assert client.get("/api/audiences").json()[0]["edited_by_user"] == 1


def test_carried_items_still_need_acknowledging_but_are_counted_once(make_client):
    unmapped = proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "prepaid"}],
                        terms=["churn_risk"], unmapped=["called the call centre"])
    client = make_client(ScriptedProvider(unmapped, delta(add=[{"attribute": "region", "op": "eq", "value": "Coast"}])))
    r = client.post("/api/interpret", json={"request": "prepaid likely to churn who called the call centre"}).json()
    refined = refine(client, r, "only the Coast").json()
    assert refined["unmapped_phrases"] == ["called the call centre"]
    assert [c["term"] for c in refined["clarifications"]] == ["churn_risk"]

    insights = client.get("/api/insights").json()
    assert insights["refinements"] == 1 and insights["requests"] == 1
    assert insights["unmapped_demand"][0]["count"] == 1
    assert next(t for t in insights["open_terms"] if t["term"] == "churn_risk")["asked"] == 1


def test_a_refinement_that_adds_a_blocked_field_is_shown_and_not_approvable(make_client):
    client = make_client(ScriptedProvider(
        PREPAID_COAST, delta(add=[{"attribute": "credit_risk_band", "op": "eq", "value": "low"}])))
    r = client.post("/api/interpret", json={"request": "prepaid in the coast"}).json()
    refined = refine(client, r, "only low credit risk").json()
    assert refined["status"] == "invalid"
    assert "credit_risk_band" in [c["attribute"] for c in refined["definition"]["conditions"]]
    assert any(i["code"] == "policy_violation" for i in refined["issues"])
    assert approve(client, refined).status_code == 422


def test_a_bad_new_condition_gets_one_repair_attempt(make_client):
    provider = ScriptedProvider(PREPAID_COAST, delta(add=[{"attribute": "favourite_colour", "op": "eq", "value": "red"}]),
                                delta(unmapped=["favourite colour"]))
    client = make_client(provider)
    r = client.post("/api/interpret", json={"request": "prepaid in the coast"}).json()
    refined = refine(client, r, "only people whose favourite colour is red").json()
    assert len(provider.calls) == 3 and refined["telemetry"]["repaired"] is True
    assert refined["status"] == "needs_confirmation" and refined["unmapped_phrases"] == ["favourite colour"]
    assert refined["changes"] == {"added": [], "removed": [], "channel": None}
