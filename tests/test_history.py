"""Past campaigns as evidence: generated from known effects, measured back, always labelled synthetic."""

import asyncio

import pytest

from app.history import Z95, CampaignHistory
from app.main import build_services
from app.mcp_server import build_server
from app.models import AudienceDefinition, Condition
from app.synthdata import CAMPAIGN_TYPES, HISTORY_CAMPAIGNS
from tests.conftest import ScriptedProvider, proposal

PREPAID = proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "prepaid"}])


@pytest.fixture(scope="module")
def history(engine, catalog):
    return CampaignHistory(engine, catalog)


def test_the_history_is_complete_and_labelled_synthetic(history, engine):
    assert history.available and history.synthetic
    assert sum(t["campaigns"] for t in history.types()) == HISTORY_CAMPAIGNS
    held_out = engine.connection.execute("SELECT avg(held_out::int) FROM campaign_contacts").fetchone()[0]
    assert abs(held_out - 0.10) < 0.01
    no_consent = engine.connection.execute("""
        SELECT count(*) FROM campaign_contacts x JOIN campaigns k USING (campaign_id) JOIN profiles p USING (profile_id)
        WHERE (k.channel = 'sms' AND NOT p.marketing_consent_sms) OR (k.channel = 'email' AND NOT p.marketing_consent_email)
    """).fetchone()[0]
    assert no_consent == 0


@pytest.mark.parametrize("campaign_type", list(CAMPAIGN_TYPES))
def test_measured_lift_recovers_the_true_effect(history, campaign_type):
    pooled = history.evidence(campaign_type)["pooled"]
    se = (pooled["lift_95_high"] - pooled["lift_95_low"]) / (2 * Z95)
    true_points = 100 * CAMPAIGN_TYPES[campaign_type]["lift"]
    assert abs(pooled["lift_points"] - true_points) < 3 * se


def test_contact_pressure_matches_a_direct_count(history, engine):
    prepaid = AudienceDefinition(conditions=[Condition(attribute="line_type", op="eq", value="prepaid")])
    pressure = history.contact_pressure(prepaid)
    direct = engine.connection.execute("""
        SELECT count(DISTINCT x.profile_id) FROM campaign_contacts x JOIN campaigns k USING (campaign_id)
        JOIN profiles p USING (profile_id), history_meta h
        WHERE NOT x.held_out AND k.sent_at > h.as_of - 7 AND p.line_type = 'prepaid' AND p.marketing_consent_sms
    """).fetchone()[0]
    assert pressure.contacted_recently == direct
    assert pressure.reachable == engine.size(prepaid).reachable
    assert pressure.synthetic and pressure.policy == "contact_policy v1"


def test_sizing_and_approval_carry_the_evidence(make_client):
    client = make_client(ScriptedProvider(PREPAID))
    r = client.post("/api/interpret", json={"request": "prepaid customers"}).json()
    sized = client.post("/api/size", json=r["definition"]).json()
    assert sized["contact_pressure"]["synthetic"] is True

    catalogue = client.get("/api/history").json()
    assert catalogue["synthetic"] is True and {t["id"] for t in catalogue["types"]} == set(CAMPAIGN_TYPES)
    assert client.get("/api/history/evidence/nope").status_code == 404
    evidence = client.get("/api/history/evidence/winback").json()
    assert evidence["synthetic"] is True and "not a forecast" in evidence["reading"]

    body = {"request_id": r["request_id"], "name": "Prepaid", "approver": "Ana", "goal": "Win back lapsed prepaid",
            "definition": r["definition"]}
    assert client.post("/api/approve", json=body | {"campaign_type": "made_up"}).status_code == 422
    m = client.post("/api/approve", json=body | {"campaign_type": "winback"}).json()["measurement"]
    assert m["campaign_type"] == "winback"
    assert m["evidence_at_approval"]["lift_points"] == evidence["pooled"]["lift_points"]
    assert m["contact_pressure_at_approval"]["contacted_recently"] == sized["contact_pressure"]["contacted_recently"]


def test_agents_get_the_same_evidence(settings):
    server = build_server(build_services(settings, ScriptedProvider()))
    out = asyncio.run(server.call_tool("past_campaign_evidence", {"campaign_type": "app_adoption"})).structured_content
    assert out["synthetic"] is True and len(out["recent_campaigns"]) == 5
    missing = asyncio.run(server.call_tool("past_campaign_evidence", {"campaign_type": "x"})).structured_content
    assert "topup_reminder" in missing["known_types"]
