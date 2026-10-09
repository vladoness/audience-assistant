import json

from tests.conftest import ScriptedProvider, proposal

PREPAID_COAST = proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "prepaid"},
                                     {"attribute": "region", "op": "eq", "value": "Coast"}])


def test_interpret_size_approve_and_audit_trail(make_client):
    client = make_client(ScriptedProvider(PREPAID_COAST))
    r = client.post("/api/interpret", json={"request": "prepaid in coast"}).json()
    size = client.post("/api/size", json=r["definition"]).json()
    approved = client.post("/api/approve", json={"request_id": r["request_id"], "name": "Coast prepaid",
                                                 "approver": "Ana (Marketing Ops)", "definition": r["definition"]})
    assert approved.status_code == 200
    assert approved.json()["reachable"] == size["reachable"]

    [audit] = client.get("/api/audiences").json()
    assert audit["request_text"] == "prepaid in coast"
    assert audit["model"] == "scripted-model"
    assert audit["edited_by_user"] == 0
    assert set(json.loads(audit["governance_versions"])) == {"catalog", "glossary", "policies", "parameters"}
    assert audit["governance_changes"] == []
    assert audit["reachable_now"] == size["reachable"]


def test_user_edits_are_recorded_as_edits(make_client):
    client = make_client(ScriptedProvider(PREPAID_COAST))
    r = client.post("/api/interpret", json={"request": "prepaid in coast"}).json()
    edited = r["definition"] | {"conditions": r["definition"]["conditions"][:1]}
    client.post("/api/approve", json={"request_id": r["request_id"], "name": "All prepaid",
                                      "approver": "Ana", "definition": edited})
    assert client.get("/api/audiences").json()[0]["edited_by_user"] == 1


def test_approval_requires_acknowledging_unmapped_phrases(make_client):
    client = make_client(ScriptedProvider(proposal(conditions=[{"attribute": "plan_tier", "op": "eq", "value": "premium"}],
                                                   unmapped=["called the call centre"])))
    r = client.post("/api/interpret", json={"request": "premium who called the call centre"}).json()
    body = {"request_id": r["request_id"], "name": "Premium", "approver": "Ana", "definition": r["definition"]}
    assert client.post("/api/approve", json=body).status_code == 422
    assert client.post("/api/approve", json=body | {"acknowledged_unmapped": True}).status_code == 200


def test_server_rejects_a_tampered_definition_at_approval(make_client):
    client = make_client(ScriptedProvider(PREPAID_COAST))
    r = client.post("/api/interpret", json={"request": "prepaid in coast"}).json()
    tampered = {"channel": "sms", "conditions": [{"attribute": "credit_risk_band", "op": "eq", "value": "high"}]}
    response = client.post("/api/approve", json={"request_id": r["request_id"], "name": "Risky", "approver": "Ana",
                                                 "definition": tampered})
    assert response.status_code == 422
    assert client.get("/api/audiences").json() == []


def test_health_and_metrics(make_client):
    client = make_client(ScriptedProvider(PREPAID_COAST))
    client.post("/api/interpret", json={"request": "prepaid in coast"})
    assert client.get("/healthz").json()["status"] == "ok"
    metrics = client.get("/api/metrics").json()
    assert metrics["counters"]["interpret_ready"] == 1
    assert metrics["timings"]["interpret_ms"]["count"] == 1
