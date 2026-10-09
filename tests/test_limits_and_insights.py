import logging
from datetime import date

from app.limits import DailyBudget, RateLimiter
from tests.conftest import ScriptedProvider, proposal


def test_rate_limiter_blocks_bursts_per_client():
    limiter = RateLimiter(per_minute=2)
    assert limiter.allow("a")[0] and limiter.allow("a")[0]
    allowed, retry_after = limiter.allow("a")
    assert not allowed and retry_after > 0
    assert limiter.allow("b")[0]


def test_interpret_endpoint_returns_429_when_rate_limited(make_client):
    provider = ScriptedProvider(proposal(conditions=[{"attribute": "roaming_30d", "op": "eq", "value": True}]))
    client = make_client(provider, rate_limit_per_min=1)
    assert client.post("/api/interpret", json={"request": "roamers"}).status_code == 200
    blocked = client.post("/api/interpret", json={"request": "roamers again"})
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers


def test_daily_budget_stops_model_calls_and_resets_next_day():
    today = [date(2026, 10, 9)]
    budget = DailyBudget(0.01, today=lambda: today[0])
    budget.spend(0.02)
    assert not budget.available()
    today[0] = date(2026, 10, 10)
    assert budget.available() and budget.spent_today == 0


def test_exhausted_budget_degrades_to_manual_builder(make_client):
    provider = ScriptedProvider(proposal(conditions=[{"attribute": "roaming_30d", "op": "eq", "value": True}]))
    r = make_client(provider, daily_budget_usd=0.0).post("/api/interpret", json={"request": "roamers"}).json()
    assert r["status"] == "ai_unavailable"
    assert r["issues"][0]["code"] == "budget_exhausted"
    assert provider.calls == []


def test_llm_calls_are_logged_with_opentelemetry_genai_attributes(make_client, caplog):
    provider = ScriptedProvider(proposal(conditions=[{"attribute": "roaming_30d", "op": "eq", "value": True}]))
    with caplog.at_level(logging.INFO, logger="app.interpret"):
        make_client(provider).post("/api/interpret", json={"request": "roamers"})
    [record] = [r for r in caplog.records if r.getMessage() == "llm_call"]
    attrs = vars(record)
    assert attrs["gen_ai.provider.name"] == "scripted"
    assert attrs["gen_ai.request.model"] == "scripted-model"
    assert attrs["gen_ai.usage.input_tokens"] == 100
    assert attrs["gen_ai.usage.output_tokens"] == 20


def test_insights_count_unmapped_demand_and_open_term_choices(make_client):
    provider = ScriptedProvider(
        proposal(conditions=[{"attribute": "plan_tier", "op": "eq", "value": "premium"}], unmapped=["called the call centre"]),
        proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "prepaid"}], unmapped=["Called the call-centre!"]),
        proposal(conditions=[{"attribute": "region", "op": "eq", "value": "North"}], terms=["active"]),
    )
    client = make_client(provider)
    client.post("/api/interpret", json={"request": "premium who called the call centre"})
    client.post("/api/interpret", json={"request": "prepaid who called the call-centre"})
    r = client.post("/api/interpret", json={"request": "active in the north"}).json()
    checked = client.post("/api/check", json={"definition": r["definition"], "pending_terms": ["active"],
                                              "resolutions": {"active": "active_usage"}}).json()
    client.post("/api/approve", json={"request_id": r["request_id"], "name": "North active", "approver": "Ana",
                                      "definition": checked["definition"]})

    insights = client.get("/api/insights").json()
    assert insights["unmapped_demand"][0]["phrase"] == "called the call centre"
    assert insights["unmapped_demand"][0]["count"] == 2
    active = next(t for t in insights["open_terms"] if t["term"] == "active")
    assert active["asked"] == 1
    assert sum(active["chosen"].values()) == 1
