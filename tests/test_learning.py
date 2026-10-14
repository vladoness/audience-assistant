"""The learning loop: outcomes are measured against the approved holdout, join the evidence, and the next
decision of the same type sees them."""

from datetime import date, timedelta

import pytest

from app.history import counts_lift, verdict
from tests.conftest import ScriptedProvider, proposal

PREPAID = proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "prepaid"}])
TOKEN = {"X-Outcome-Token": "campaign-tool-secret"}


@pytest.fixture
def reporting_client(make_client):
    """Outcome reporting on, and the clock 20 days ahead so a send made today is past its 14-day window."""
    def _make(*proposals):
        client = make_client(ScriptedProvider(*proposals), outcome_token=TOKEN["X-Outcome-Token"])
        client.app.state.services.today = lambda: date.today() + timedelta(days=20)
        return client
    return _make


def report(client, audience_id, responders, **body):
    return client.post(f"/api/audiences/{audience_id}/outcome", headers=TOKEN,
                       json={"responders": responders, "sent_at": date.today().isoformat(), **body})


def approve(client, campaign_type="winback", name="Prepaid win-back"):
    r = client.post("/api/interpret", json={"request": "prepaid customers"}).json()
    body = {"request_id": r["request_id"], "name": name, "approver": "Ana", "goal": "Win back lapsed prepaid",
            "definition": r["definition"]}
    if campaign_type:
        body["campaign_type"] = campaign_type
    return client.post("/api/approve", json=body).json()


def members(client, audience_id, group="all"):
    rows = client.get(f"/api/audiences/{audience_id}/export.csv?group={group}").text.strip().splitlines()[1:]
    return [line.split(",") for line in rows]


def confirm(client, audience_id):
    return client.post(f"/api/audiences/{audience_id}/outcome/confirm", json={"confirmed_by": "Ana"})


def test_reported_responders_are_counted_against_the_approved_groups(reporting_client):
    client = reporting_client(PREPAID)
    a = approve(client)
    groups = members(client, a["id"])
    contacted = [p for p, g in groups if g == "contacted"]
    held_out = [p for p, g in groups if g == "held_out"]
    responders = contacted[:300] + held_out[:10] + ["L99999999"]
    out = report(client, a["id"], responders).json()
    m = out["measured"]
    assert (m["contacted"], m["held_out"]) == (len(contacted), len(held_out))
    assert m["contacted_response_pct"] == round(100 * 300 / len(contacted), 2)
    assert out["unknown_responders"] == 1 and out["current_data_matches_approval"] is True
    assert out["status"] == "pending"
    assert report(client, 999, []).status_code == 404


def test_the_export_is_the_contacted_group_unless_asked_and_matches_the_approval(reporting_client):
    client = reporting_client(PREPAID)
    a = approve(client)
    contacted = members(client, a["id"], "contacted")
    held_out = members(client, a["id"], "held_out")
    default = client.get(f"/api/audiences/{a['id']}/export.csv")
    assert default.text.strip().splitlines()[1:] == [",".join(r) for r in contacted]
    assert {g for _, g in contacted} == {"contacted"} and {g for _, g in held_out} == {"held_out"}
    assert (len(contacted), len(held_out)) == (a["measurement"]["target_lines"], a["measurement"]["holdout_lines"])
    assert default.headers["x-membership"] == "as approved"


def test_outcome_reports_need_the_token(make_client):
    client = make_client(ScriptedProvider(PREPAID, PREPAID))
    a = approve(client)
    assert client.post(f"/api/audiences/{a['id']}/outcome", json={"responders": []}).status_code == 403
    client = make_client(ScriptedProvider(PREPAID), outcome_token="secret")
    a = approve(client)
    assert client.post(f"/api/audiences/{a['id']}/outcome", json={"responders": []}).status_code == 401
    assert client.post(f"/api/audiences/{a['id']}/outcome", headers={"X-Outcome-Token": "wrong"},
                       json={"responders": []}).status_code == 401


def test_implausible_reports_are_rejected_before_they_reach_the_evidence(reporting_client):
    client = reporting_client(PREPAID)
    a = approve(client)
    contacted = [p for p, _ in members(client, a["id"], "contacted")]
    today = date.today()
    assert report(client, a["id"], [], sent_at=(today - timedelta(days=1)).isoformat()).status_code == 422  # before approval
    assert report(client, a["id"], [], sent_at=(today + timedelta(days=30)).isoformat()).status_code == 422  # future
    client.app.state.services.today = lambda: today + timedelta(days=5)
    assert "window" in report(client, a["id"], contacted[:10]).json()["detail"]  # still being measured
    client.app.state.services.today = lambda: today + timedelta(days=20)
    assert "right file" in report(client, a["id"], contacted[:10] + [f"X{i}" for i in range(20)]).json()["detail"]
    assert "plausible" in report(client, a["id"], contacted).json()["detail"]  # everyone responded
    assert client.get("/api/learning").json()["scorecard"] == []


def test_a_pending_outcome_can_be_replaced_and_only_a_confirmed_one_is_learned_from(reporting_client):
    client = reporting_client(PREPAID)
    a = approve(client)
    before = client.get("/api/history/evidence/winback").json()["campaigns"]
    contacted = [p for p, _ in members(client, a["id"], "contacted")]
    report(client, a["id"], contacted[:50])
    second = report(client, a["id"], contacted[:300]).json()
    assert second["reports"] == 2 and second["measured"]["contacted_response_pct"] > 0.5
    assert client.get("/api/history/evidence/winback").json()["campaigns"] == before  # pending: not evidence yet
    assert client.get("/api/learning").json()["scorecard"][0]["status"] == "pending"
    assert confirm(client, a["id"]).status_code == 200
    assert client.get("/api/history/evidence/winback").json()["campaigns"] == before + 1
    assert confirm(client, a["id"]).status_code == 409
    assert report(client, a["id"], contacted[:10]).status_code == 409  # confirmed outcomes can't be overwritten


def test_a_measured_campaign_joins_the_evidence_the_next_decision_sees(make_client):
    client = make_client(ScriptedProvider(PREPAID, PREPAID))
    first = approve(client)
    before = first["measurement"]["evidence_at_approval"]["campaigns"]
    assert client.post(f"/api/audiences/{first['id']}/simulate-outcome").status_code == 200
    assert confirm(client, first["id"]).status_code == 200
    evidence = client.get("/api/history/evidence/winback").json()
    assert evidence["campaigns"] == before + 1 and evidence["measured_here"] == 1
    assert any(c["source"] == "approved here (simulated send)" for c in evidence["per_campaign"])
    second = approve(client, name="Second wave")
    assert second["measurement"]["evidence_at_approval"]["campaigns"] == before + 1


def test_the_learning_curve_ends_with_the_campaign_measured_here_and_narrows(make_client):
    client = make_client(ScriptedProvider(PREPAID))
    a = approve(client)
    client.post(f"/api/audiences/{a['id']}/simulate-outcome")
    confirm(client, a["id"])
    timeline = client.get("/api/learning/winback").json()["timeline"]
    assert timeline[-1]["audience_id"] == a["id"]
    assert [p["sent_at"] for p in timeline] == sorted(p["sent_at"] for p in timeline)
    width = lambda p: p["estimate_high"] - p["estimate_low"]
    assert width(timeline[-1]) < width(timeline[0])

    summary = client.get("/api/learning").json()
    assert summary["loop"] == {"interpreted": 1, "approved": 1, "approved_with_type": 1, "measured": 1,
                               "pending_confirmation": 0}
    assert summary["scorecard"][0]["audience_id"] == a["id"] and summary["scorecard"][0]["expected"]


def test_simulation_needs_a_campaign_type_and_the_synthetic_history(make_client):
    client = make_client(ScriptedProvider(PREPAID))
    a = approve(client, campaign_type=None)
    assert client.post(f"/api/audiences/{a['id']}/simulate-outcome").status_code == 422
    client.app.state.services.history.synthetic = False
    b = approve(client)
    assert client.post(f"/api/audiences/{b['id']}/simulate-outcome").status_code == 409


def test_the_approver_is_told_before_approving_when_the_holdout_cannot_show_a_typical_lift(make_client):
    client = make_client(ScriptedProvider(PREPAID))
    definition = {"channel": "sms", "conditions": [{"attribute": "line_type", "op": "eq", "value": "prepaid"}]}
    weak = client.post("/api/measurement-preview", json={"definition": definition, "campaign_type": "app_adoption"}).json()
    assert weak["available"] and weak["past_lift_points"] < weak["detectable_lift_points"]
    assert "too small to tell" in weak["power_note"]
    a = approve(client, campaign_type="app_adoption")
    assert a["measurement"]["power_note"] and a["measurement"]["detectable_lift_points"] == pytest.approx(
        weak["detectable_lift_points"], rel=0.1)


def test_a_difference_is_only_claimed_when_the_intervals_do_not_overlap():
    expected = {"lift_points": 1.0, "lift_95_low": 0.8, "lift_95_high": 1.2}
    consistent = counts_lift(20_000, 2000, 400, 20)  # +1.0, interval clear of zero and overlapping the expectation
    assert verdict(expected, consistent, 500) == "in line with past campaigns"
    clearly_worse = counts_lift(200_000, 20_000, 2000, 200)  # lift ~0, tight interval
    assert verdict(expected, clearly_worse, 500) == "below what past campaigns predicted"
    clearly_better = counts_lift(200_000, 20_000, 10_000, 200)
    assert verdict(expected, clearly_better, 500) == "above what past campaigns predicted"
    assert verdict(None, consistent) == "no expectation was recorded at approval"


def test_a_result_that_cannot_be_told_from_no_effect_is_never_called_in_line():
    expected = {"lift_points": 1.0, "lift_95_low": 0.8, "lift_95_high": 1.2}
    noisy = counts_lift(20_000, 2000, 300, 20)  # +0.5, interval includes zero
    assert verdict(expected, noisy, 500).startswith("too small to tell (the interval")
    small = counts_lift(2000, 200, 30, 2)
    assert verdict(expected, small, 500).startswith("too small to tell (fewer than 500")


def test_a_group_with_no_responders_does_not_get_an_exact_interval():
    low, high = counts_lift(900, 100, 0, 0).interval
    assert low < 0 < high and high - low > 1
