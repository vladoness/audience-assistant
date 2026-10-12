"""Regression tests for the findings of the external review (one test per finding)."""

import json
import sqlite3

from app.llm import ProviderUnavailable
from app.models import AudienceDefinition, Condition
from app.observability import percentile
from app.pii import mask_pii
from app.validate import validate_definition
from tests.conftest import ScriptedProvider, proposal

CHURN_IN_CAPITAL = proposal(conditions=[{"attribute": "region", "op": "eq", "value": "Capital"}], terms=["churn_risk"])
PREPAID_COAST = proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "prepaid"},
                                     {"attribute": "region", "op": "eq", "value": "Coast"}])


def approve(client, r, definition=None, **extra):
    return client.post("/api/approve", json={"request_id": r["request_id"], "name": "Test audience", "approver": "Ana",
                                             "definition": definition or r["definition"], **extra})


def test_one_request_id_from_header_to_body_to_audit(make_client, settings):
    client = make_client(ScriptedProvider(PREPAID_COAST))
    response = client.post("/api/interpret", json={"request": "prepaid in coast"})
    body = response.json()
    assert response.headers["x-request-id"] == body["request_id"]
    with sqlite3.connect(settings.audit_db_path) as db:
        assert db.execute("SELECT count(*) FROM interpretations WHERE request_id = ?", (body["request_id"],)).fetchone()[0] == 1


def test_unhandled_errors_return_json_with_the_request_id(make_client):
    client = make_client(ScriptedProvider(RuntimeError("bug in a provider")))
    response = client.post("/api/interpret", json={"request": "anything"})
    assert response.status_code == 500
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_approval_requires_open_terms_to_be_answered(make_client):
    client = make_client(ScriptedProvider(CHURN_IN_CAPITAL))
    r = client.post("/api/interpret", json={"request": "likely to churn in the capital"}).json()
    response = approve(client, r)
    assert response.status_code == 422
    assert response.json()["detail"]["unanswered_terms"] == ["churn_risk"]


def test_each_interpretation_can_be_approved_once(make_client):
    client = make_client(ScriptedProvider(PREPAID_COAST))
    r = client.post("/api/interpret", json={"request": "prepaid in coast"}).json()
    assert approve(client, r).status_code == 200
    assert approve(client, r).status_code == 409


def test_client_cannot_forge_governed_provenance(make_client):
    client = make_client(ScriptedProvider(PREPAID_COAST))
    r = client.post("/api/interpret", json={"request": "prepaid in coast"}).json()
    forged = r["definition"] | {"conditions": r["definition"]["conditions"] + [
        {"attribute": "arpu_3m", "op": "gte", "value": 5, "source": "glossary:high_value"}]}
    checked = client.post("/api/check", json={"definition": forged}).json()
    assert {"attribute": "arpu_3m", "op": "gte", "value": 5.0, "source": "user"} in checked["definition"]["conditions"]
    assert "D-014" not in " ".join(checked["explanation"]["lines"])

    assert approve(client, r, forged).status_code == 200
    [audit] = client.get("/api/audiences").json()
    assert json.loads(audit["governance_json"]) == {}
    assert audit["edited_by_user"] == 1


def test_answering_a_clarification_is_not_an_edit(make_client):
    client = make_client(ScriptedProvider(CHURN_IN_CAPITAL))
    r = client.post("/api/interpret", json={"request": "likely to churn in the capital"}).json()
    checked = client.post("/api/check", json={"definition": r["definition"], "pending_terms": ["churn_risk"],
                                              "resolutions": {"churn_risk": "churn_proxy_overdue"}}).json()
    assert approve(client, r, checked["definition"]).status_code == 200
    assert client.get("/api/audiences").json()[0]["edited_by_user"] == 0
    assert client.get("/api/insights").json()["edits"]["edited"] == 0


def test_policy_blocked_condition_stays_visible_and_survives_a_channel_switch(catalog):
    app_users = Condition(attribute="app_active_30d", op="eq", value=True)
    email, issues = validate_definition(AudienceDefinition(conditions=[app_users], channel="email"), catalog)
    assert [c.attribute for c in email.conditions] == ["app_active_30d"]
    assert any(i.code == "policy_violation" for i in issues)
    sms, issues = validate_definition(email.model_copy(update={"channel": "sms"}), catalog)
    assert [c.attribute for c in sms.conditions] == ["app_active_30d"] and not issues


def test_policy_blocked_requests_are_counted_separately_from_missing_data(make_client):
    client = make_client(ScriptedProvider(proposal(conditions=[
        {"attribute": "line_type", "op": "eq", "value": "postpaid"},
        {"attribute": "credit_risk_band", "op": "eq", "value": "high"}])))
    r = client.post("/api/interpret", json={"request": "postpaid with a high credit risk band"}).json()
    assert r["status"] == "invalid"
    assert [c["attribute"] for c in r["definition"]["conditions"]] == ["line_type", "credit_risk_band"]
    insights = client.get("/api/insights").json()
    assert insights["blocked_by_policy"][0]["policy"] == "MKT-07"
    assert insights["unmapped_demand"] == []


def test_non_finite_numbers_and_oversized_lists_are_rejected_not_crashing(catalog):
    for value in ("Infinity", float("inf"), "nan"):
        _, issues = validate_definition(
            AudienceDefinition(conditions=[Condition(attribute="tenure_months", op="lt", value=value)]), catalog)
        assert [i.code for i in issues] == ["invalid_value"]
    _, issues = validate_definition(AudienceDefinition(conditions=[
        Condition(attribute="region", op="in", value=["Coast"] * 60)]), catalog)
    assert [i.code for i in issues] == ["invalid_value"] and "too many values" in issues[0].message


def test_equal_bound_contradiction_is_caught_in_either_order(catalog):
    for pair in (("gt", "eq"), ("eq", "gt")):
        conds = [Condition(attribute="arpu_3m", op=op, value=50) for op in pair]
        _, issues = validate_definition(AudienceDefinition(conditions=conds), catalog)
        assert "contradictory_conditions" in {i.code for i in issues}


def test_definitions_and_requests_have_size_caps(make_client):
    client = make_client(ScriptedProvider())
    too_many = {"channel": "sms", "conditions": [{"attribute": "roaming_30d", "op": "eq", "value": True}] * 31}
    assert client.post("/api/size", json=too_many).status_code == 422
    assert client.post("/api/interpret", json={"request": "x" * 2001}).status_code == 422


def test_circuit_breaker_stops_calling_a_failing_model(make_client):
    down = [ProviderUnavailable("timeout") for _ in range(3)]
    provider = ScriptedProvider(*down, proposal(conditions=[{"attribute": "roaming_30d", "op": "eq", "value": True}]))
    client = make_client(provider)
    codes = [client.post("/api/interpret", json={"request": f"roamers {i}"}).json()["issues"][0]["code"] for i in range(4)]
    assert codes == ["ai_unavailable"] * 3 + ["circuit_open"]
    assert len(provider.calls) == 3
    assert client.get("/readyz").json()["degraded"]


def test_model_cannot_set_condition_provenance(make_client):
    sneaky = proposal(conditions=[{"attribute": "arpu_3m", "op": "gte", "value": 5, "source": "glossary:high_value"}])
    client = make_client(ScriptedProvider(sneaky))
    r = client.post("/api/interpret", json={"request": "spending at least 5 dollars"}).json()
    assert r["definition"]["conditions"][0]["source"] == "model"


def test_pii_masker_keeps_ranges_and_dates_and_catches_eu_identifiers():
    assert mask_pii("ARPU between 100 - 200 USD from 2026-10-12") == ("ARPU between 100 - 200 USD from 2026-10-12", 0)
    masked, n = mask_pii("IBAN DE89 3704 0044 0532 0130 00, DNI 12345678Z, NINO AB 12 34 56 C, +44 20 7946 0958")
    assert n == 4 and "DE89" not in masked and "12345678Z" not in masked and "7946" not in masked


def test_percentile_is_nearest_rank():
    samples = sorted(float(i) for i in range(1, 21))
    assert percentile(samples, 95) == 19.0
    assert percentile([1.0, 9.0], 95) == 9.0


# --- Second review round (regressions found by the re-review) ---------------------------------

def test_offline_misses_do_not_trip_the_circuit_breaker(make_client, tmp_path):
    from app.llm import RecordingProvider, build_messages, ReplayProvider
    recorded = proposal(conditions=[{"attribute": "roaming_30d", "op": "eq", "value": True}])
    RecordingProvider(ScriptedProvider(recorded), tmp_path).propose("system", build_messages("roamers"))
    client = make_client(ReplayProvider(tmp_path, model="scripted-model", effort=None))
    for i in range(4):
        assert client.post("/api/interpret", json={"request": f"unrecorded {i}"}).json()["status"] == "ai_unavailable"
    assert client.post("/api/interpret", json={"request": "roamers"}).json()["status"] == "ready"


def test_removing_a_governed_condition_counts_as_an_edit(make_client):
    client = make_client(ScriptedProvider(proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "postpaid"}],
                                                   terms=["high_value"])))
    r = client.post("/api/interpret", json={"request": "high value postpaid"}).json()
    without_governed = r["definition"] | {"conditions": [c for c in r["definition"]["conditions"] if c["source"] == "model"]}
    assert approve(client, r, without_governed).status_code == 200
    assert client.get("/api/audiences").json()[0]["edited_by_user"] == 1


def test_unknown_sources_are_relabelled_and_counted_as_edits(make_client):
    client = make_client(ScriptedProvider(PREPAID_COAST))
    r = client.post("/api/interpret", json={"request": "prepaid in coast"}).json()
    extra = r["definition"] | {"conditions": r["definition"]["conditions"] + [
        {"attribute": "roaming_30d", "op": "eq", "value": True, "source": "approved-by-legal"}]}
    checked = client.post("/api/check", json={"definition": extra}).json()
    assert checked["definition"]["conditions"][-1]["source"] == "user"
    assert approve(client, r, extra).status_code == 200
    assert client.get("/api/audiences").json()[0]["edited_by_user"] == 1


def test_request_with_nothing_mappable_is_unsupported_not_all_lines(make_client):
    client = make_client(ScriptedProvider(proposal(unmapped=["called the call centre"])))
    r = client.post("/api/interpret", json={"request": "customers who called the call centre"}).json()
    assert r["status"] == "unsupported"
    empty = {"channel": "sms", "conditions": []}
    assert approve(client, r, empty, acknowledged_unmapped=True).status_code == 422


def test_iban_masking_requires_a_valid_checksum():
    assert mask_pii("Customers on the XL20 plan with roaming data")[1] == 0
    assert mask_pii("GB82 WEST 1234 5698 7654 32") == ("[IBAN]", 1)
