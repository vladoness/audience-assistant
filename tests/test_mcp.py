"""MCP server: agents can draft, change, size and explain audiences; only a person approves."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.main import build_services, create_app
from app.mcp_server import build_server
from tests.conftest import ScriptedProvider, proposal

ACTIVE_COAST = proposal(conditions=[{"attribute": "region", "op": "eq", "value": "Coast"}], terms=["active"])
PREPAID_COAST = proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "prepaid"},
                                     {"attribute": "region", "op": "eq", "value": "Coast"}])
REFINE_TO_NORTH = {"add_conditions": [{"attribute": "region", "op": "eq", "value": "North"}], "remove_attributes": [],
                   "add_glossary_terms": [], "remove_glossary_terms": [], "channel": "unchanged",
                   "unmapped_phrases": [], "no_change": False, "assumptions": []}


@pytest.fixture
def mcp(settings):
    def _make(*responses):
        services = build_services(settings, ScriptedProvider(*responses))
        return services, build_server(services)
    return _make


def call(server, tool, **args):
    result = asyncio.run(server.call_tool(tool, args))
    return result.structured_content


def test_agents_get_drafting_tools_but_no_way_to_approve(mcp):
    _, server = mcp()
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {"interpret_audience", "refine_audience", "size_audience", "explain_term",
                          "past_campaign_evidence", "advise_audience"}
    assert tools["past_campaign_evidence"].annotations.read_only_hint is True
    assert tools["size_audience"].annotations.read_only_hint is True
    assert tools["interpret_audience"].annotations.destructive_hint is False
    assert [str(r.uri) for r in asyncio.run(server.list_resources())] == ["audience://catalog"]


def test_a_drafted_audience_is_audited_as_mcp_and_handed_to_a_person(mcp, settings):
    services, server = mcp(ACTIVE_COAST)
    out = call(server, "interpret_audience", request="active customers in the Coast")
    assert out["status"] == "needs_clarification"
    assert out["questions"][0]["term"] == "active" and len(out["questions"][0]["options"]) == 2
    assert all(o["reachable"] is not None for o in out["questions"][0]["options"])  # consequences go to agents too
    assert out["review_url"].endswith(f"/?review={out['request_id']}")
    assert json.loads(services.store.get_interpretation(out["request_id"])["telemetry_json"])["origin"] == "mcp"

    # The person opens the review link in the web app, answers the question and approves there.
    with TestClient(create_app(settings, ScriptedProvider())) as client:
        review = client.get(f"/api/interpretations/{out['request_id']}").json()
        assert review["status"] == "needs_clarification" and review["telemetry"]["origin"] == "mcp"
        assert review["request_text"] == "active customers in the Coast"
        checked = client.post("/api/check", json={"definition": review["definition"], "pending_terms": ["active"],
                                                  "resolutions": {"active": "active_usage"}}).json()
        approved = client.post("/api/approve", json={"request_id": out["request_id"], "name": "Active Coast",
                                                     "approver": "Ana", "goal": "Test campaign", "definition": checked["definition"]})
        assert approved.status_code == 200
        assert client.get("/api/interpretations/nope").status_code == 404


def test_refine_applies_a_change_to_the_stored_draft(mcp):
    _, server = mcp(PREPAID_COAST, REFINE_TO_NORTH)
    first = call(server, "interpret_audience", request="prepaid in the Coast")
    out = call(server, "refine_audience", request_id=first["request_id"], change="the North instead")
    assert out["request_id"] != first["request_id"]
    assert out["changes"] == {"added": ["Region is North"], "removed": ["Region is Coast"], "channel": None}
    assert "error" in call(server, "refine_audience", request_id="nope", change="drop the region")


def test_sizing_is_refused_while_questions_are_open(mcp):
    _, server = mcp(ACTIVE_COAST, PREPAID_COAST)
    open_draft = call(server, "interpret_audience", request="active customers in the Coast")
    refused = call(server, "size_audience", request_id=open_draft["request_id"])
    assert refused["status"] == "needs_clarification" and "review_url" in refused

    ready = call(server, "interpret_audience", request="prepaid in the Coast")
    sized = call(server, "size_audience", request_id=ready["request_id"])
    assert 0 < sized["reachable"] <= sized["matched"]
    assert sized["excluded_no_consent"] == sized["matched"] - sized["reachable"]


def test_terms_explain_their_governance(mcp):
    _, server = mcp()
    assert call(server, "explain_term", name="high value")["decision"]
    open_term = call(server, "explain_term", name="active")
    assert open_term["status"] == "open" and open_term["decided_by"].startswith("Marketing Analytics")
    assert "high_value" in call(server, "explain_term", name="loyal")["known_terms"]
