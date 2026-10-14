"""The campaign advisor: the model proposes from facts, code checks every proposal and computes its
consequence. Uses a 200k-line store so the planted segment effect has real statistical power."""

import re
from dataclasses import replace

import pytest

from app.audience import AudienceEngine
from app.history import SEGMENT_ATTRIBUTES, Z95, CampaignHistory
from app.llm import ADVICE_SCHEMA, ProviderUnavailable
from app.synthdata import CAMPAIGN_TYPES, generate, true_lift
from tests.conftest import ScriptedProvider

UPSELL = {"channel": "sms", "conditions": [{"attribute": "device_os", "op": "eq", "value": "android"},
                                           {"attribute": "data_gb_30d", "op": "gt", "value": 10}]}


@pytest.fixture(scope="module")
def big_store(tmp_path_factory):
    path = tmp_path_factory.mktemp("big") / "profiles.duckdb"
    generate(path, 200_000)
    return path


@pytest.fixture(scope="module")
def big_history(big_store, catalog):
    engine = AudienceEngine(big_store, catalog)
    return CampaignHistory(engine, catalog)


def proposal(kind, title, rationale, cites, attribute="", value="", channel="none", campaign_type=""):
    return {"kind": kind, "title": title, "rationale": rationale, "cites": cites, "attribute": attribute,
            "value": value, "channel": channel, "campaign_type": campaign_type}


def test_only_the_planted_segment_effect_is_flagged(big_history):
    flagged = {(t, a, s["value"]) for t in CAMPAIGN_TYPES for a in SEGMENT_ATTRIBUTES
               for s in big_history.segment_lift(t, a) if s["clearly_different"]}
    assert flagged == {("data_upsell", "line_type", "postpaid"), ("data_upsell", "line_type", "prepaid")}


def test_segment_lift_recovers_the_planted_truth(big_history):
    for seg in big_history.segment_lift("data_upsell", "line_type"):
        se = (seg["lift_95_high"] - seg["lift_95_low"]) / (2 * Z95)
        truth = 100 * true_lift(CAMPAIGN_TYPES["data_upsell"], {"line_type": seg["value"]})
        assert abs(seg["lift_points"] - truth) < 3 * se


def test_proposals_are_checked_against_the_facts_before_anyone_sees_them(make_client, big_store):
    scripted = {"summary": "Postpaid responds better.", "proposals": [
        proposal("narrow_segment", "Keep only postpaid lines", "Past upsells did better on postpaid lines (+1.87).",
                 ["segment.data_upsell.line_type.postpaid"], "line_type", "postpaid"),
        proposal("narrow_segment", "Focus on the Coast", "The Coast responds best.",
                 ["segment.data_upsell.region.Coast"], "region", "Coast"),
        proposal("advice", "Send twice", "Sending twice lifts response by 4.2 points.", ["type.data_upsell"]),
    ]}
    provider = ScriptedProvider(scripted)
    client = make_client(provider, data_path=big_store)
    r = client.post("/api/advise", json={"definition": UPSELL, "goal": "Upsell data", "campaign_type": "data_upsell"}).json()
    assert provider.schemas[-1] is ADVICE_SCHEMA
    assert [p["title"] for p in r["proposals"]] == ["Keep only postpaid lines"]
    reasons = {x["title"]: x["reason"] for x in r["rejected"]}
    assert "don't show that segment responding clearly differently" in reasons["Focus on the Coast"]
    assert "numbers that aren't in the facts" in reasons["Send twice"]

    c = r["proposals"][0]["consequence"]
    assert c["reachable_after"] < c["reachable_before"]
    assert c["extra_responders_after"] < c["extra_responders_before"]  # better rate, fewer responders in total
    assert c["definition"]["conditions"][-1] == {"attribute": "line_type", "op": "eq", "value": "postpaid", "source": "advisor"}

    facts = " ".join(f["text"] for f in r["facts"])
    assert not re.search(r"L\d{8}", facts), "no customer ids in the facts"
    assert "credit" not in facts.lower(), "no policy-blocked fields in the facts"


def test_an_applied_proposal_stays_labelled_and_counts_as_an_edit(make_client, big_store):
    client = make_client(ScriptedProvider(), data_path=big_store)
    applied = UPSELL | {"conditions": UPSELL["conditions"] + [
        {"attribute": "line_type", "op": "eq", "value": "postpaid", "source": "advisor"}]}
    checked = client.post("/api/check", json={"definition": applied}).json()
    assert checked["definition"]["conditions"][-1]["source"] == "advisor"


def test_blocked_channels_and_unsupported_switches_are_withheld(make_client, big_store):
    app_users = {"channel": "sms", "conditions": [{"attribute": "app_active_30d", "op": "eq", "value": True}]}
    scripted = {"summary": "", "proposals": [
        proposal("switch_channel", "Use email instead", "Email reaches more.", ["channel.email"], channel="email"),
        proposal("choose_campaign_type", "Run a win-back", "Win-backs work.", ["type.nope"], campaign_type="winback")]}
    client = make_client(ScriptedProvider(scripted), data_path=big_store)
    r = client.post("/api/advise", json={"definition": app_users, "campaign_type": "app_adoption"}).json()
    assert not r["proposals"] and len(r["rejected"]) == 2
    assert "isn't allowed" in r["rejected"][0]["reason"]


def test_the_advisor_degrades_cleanly(make_client, big_store):
    client = make_client(ScriptedProvider(ProviderUnavailable("timeout")), data_path=big_store)
    assert client.post("/api/advise", json={"definition": UPSELL}).status_code == 503
    blocked = {"channel": "sms", "conditions": [{"attribute": "credit_risk_band", "op": "eq", "value": "low"}]}
    assert client.post("/api/advise", json={"definition": blocked}).status_code == 422
