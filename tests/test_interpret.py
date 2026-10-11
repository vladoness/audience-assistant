from app.llm import ProviderUnavailable
from tests.conftest import ScriptedProvider, proposal


def interpret(make_client, provider, text="any request"):
    client = make_client(provider)
    return client, client.post("/api/interpret", json={"request": text}).json()


def test_decided_glossary_term_is_expanded_by_the_server(make_client):
    provider = ScriptedProvider(proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "postpaid"}],
                                         terms=["high_value"]))
    _, r = interpret(make_client, provider, "high value postpaid")
    assert r["status"] == "ready"
    governed = [c for c in r["definition"]["conditions"] if c["source"] == "glossary:high_value"]
    assert governed == [{"attribute": "arpu_3m", "op": "gte", "value": 40.0, "source": "glossary:high_value"}]


def test_open_term_asks_the_marketer_and_check_applies_the_choice(make_client):
    provider = ScriptedProvider(proposal(conditions=[{"attribute": "device_os", "op": "eq", "value": "android"}],
                                         terms=["active"]))
    client, r = interpret(make_client, provider, "active android users")
    assert r["status"] == "needs_clarification"
    assert [o["id"] for o in r["clarifications"][0]["options"]] == ["active_usage", "active_billing"]

    checked = client.post("/api/check", json={"definition": r["definition"], "pending_terms": ["active"],
                                              "resolutions": {"active": "active_usage"}}).json()
    assert checked["status"] == "ready"
    assert {"attribute": "usage_events_30d", "op": "gte", "value": 1, "source": "glossary:active=active_usage"} \
        in checked["definition"]["conditions"]
    assert len(provider.calls) == 1


def test_unmapped_phrases_need_confirmation_instead_of_being_dropped(make_client):
    provider = ScriptedProvider(proposal(conditions=[{"attribute": "plan_tier", "op": "eq", "value": "premium"}],
                                         unmapped=["called the call centre twice"]))
    _, r = interpret(make_client, provider)
    assert r["status"] == "needs_confirmation"
    assert r["unmapped_phrases"] == ["called the call centre twice"]


def test_invalid_proposal_gets_exactly_one_repair_attempt(make_client):
    bad = proposal(conditions=[{"attribute": "churn_score", "op": "gt", "value": 0.8}])
    fixed = proposal(conditions=[], unmapped=["likely to churn"])
    provider = ScriptedProvider(bad, fixed)
    _, r = interpret(make_client, provider)
    assert r["status"] == "needs_confirmation"
    assert r["telemetry"]["repaired"] is True
    assert len(provider.calls) == 2
    assert "churn_score" in provider.calls[1][0]["content"]


def test_still_invalid_after_repair_stops_and_reports(make_client):
    bad = proposal(conditions=[{"attribute": "churn_score", "op": "gt", "value": 0.8}])
    provider = ScriptedProvider(bad, bad, bad)
    _, r = interpret(make_client, provider)
    assert r["status"] == "invalid"
    assert len(provider.calls) == 2
    assert r["issues"][0]["code"] == "unknown_attribute"


def test_policy_violations_are_not_sent_back_for_repair(make_client):
    provider = ScriptedProvider(proposal(conditions=[{"attribute": "credit_risk_band", "op": "eq", "value": "high"}]))
    _, r = interpret(make_client, provider)
    assert r["status"] == "invalid"
    assert r["issues"][0]["code"] == "policy_violation"
    assert len(provider.calls) == 1


def test_model_outage_degrades_to_manual_builder(make_client):
    provider = ScriptedProvider(ProviderUnavailable("APITimeoutError"))
    client, r = interpret(make_client, provider)
    assert r["status"] == "ai_unavailable"
    assert r["definition"]["conditions"] == []
    sized = client.post("/api/size", json={"conditions": [{"attribute": "roaming_30d", "op": "eq", "value": True}],
                                           "channel": "sms"})
    assert sized.status_code == 200


def test_identical_requests_hit_the_cache(make_client):
    provider = ScriptedProvider(proposal(conditions=[{"attribute": "roaming_30d", "op": "eq", "value": True}]))
    client = make_client(provider)
    first = client.post("/api/interpret", json={"request": "Roamers   last month"}).json()
    second = client.post("/api/interpret", json={"request": "roamers last month"}).json()
    assert first["telemetry"]["cache_hit"] is False
    assert second["telemetry"]["cache_hit"] is True
    assert second["definition"] == first["definition"]
    assert len(provider.calls) == 1


def test_pii_is_masked_before_the_model_sees_it(make_client):
    provider = ScriptedProvider(proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "prepaid"}]))
    _, r = interpret(make_client, provider, "prepaid customers like ana.perez@mail.com")
    sent = provider.calls[0][0]["content"]
    assert "ana.perez@mail.com" not in sent and "[EMAIL]" in sent
    assert r["telemetry"]["pii_masked"] == 1


def test_non_audience_requests_are_unsupported(make_client):
    provider = ScriptedProvider(proposal(out_of_scope=True))
    _, r = interpret(make_client, provider, "write me a poem about roaming")
    assert r["status"] == "unsupported"


def test_over_long_requests_are_rejected_without_calling_the_model(make_client):
    provider = ScriptedProvider()
    _, r = interpret(make_client, provider, "x" * 600)
    assert r["status"] == "invalid"
    assert r["issues"][0]["code"] == "request_too_long"
    assert provider.calls == []


def test_glossary_term_without_textual_evidence_is_flagged(make_client):
    provider = ScriptedProvider(proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "postpaid"}],
                                         terms=["high_value"], unmapped=["high credit risk band"]))
    _, r = interpret(make_client, provider, "Postpaid customers with a high credit risk band")
    assert "ungrounded_term" in [i["code"] for i in r["issues"]]


def test_glossary_term_named_in_the_request_is_not_flagged(make_client):
    provider = ScriptedProvider(proposal(terms=["high_value"]))
    _, r = interpret(make_client, provider, "High value customers")
    assert "ungrounded_term" not in [i["code"] for i in r["issues"]]
