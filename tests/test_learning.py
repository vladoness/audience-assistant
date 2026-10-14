"""The learning loop: outcomes are measured against the approved holdout, join the evidence, and the next
decision of the same type sees them."""

from app.history import counts_lift, verdict
from tests.conftest import ScriptedProvider, proposal

PREPAID = proposal(conditions=[{"attribute": "line_type", "op": "eq", "value": "prepaid"}])


def approve(client, campaign_type="winback", name="Prepaid win-back"):
    r = client.post("/api/interpret", json={"request": "prepaid customers"}).json()
    body = {"request_id": r["request_id"], "name": name, "approver": "Ana", "goal": "Win back lapsed prepaid",
            "definition": r["definition"]}
    if campaign_type:
        body["campaign_type"] = campaign_type
    return client.post("/api/approve", json=body).json()


def members(client, audience_id):
    rows = client.get(f"/api/audiences/{audience_id}/export.csv").text.strip().splitlines()[1:]
    return [line.split(",") for line in rows]


def test_reported_responders_are_counted_against_the_approved_groups(make_client):
    client = make_client(ScriptedProvider(PREPAID))
    a = approve(client)
    groups = members(client, a["id"])
    contacted = [p for p, g in groups if g == "contacted"]
    held_out = [p for p, g in groups if g == "held_out"]
    responders = contacted[:300] + held_out[:10] + ["L99999999"]
    out = client.post(f"/api/audiences/{a['id']}/outcome", json={"responders": responders}).json()
    m = out["measured"]
    assert (m["contacted"], m["held_out"]) == (len(contacted), len(held_out))
    assert m["contacted_response_pct"] == round(100 * 300 / len(contacted), 2)
    assert out["unknown_responders"] == 1 and out["membership_matches_approval"] is True
    assert client.post(f"/api/audiences/{a['id']}/outcome", json={"responders": []}).status_code == 409
    assert client.post("/api/audiences/999/outcome", json={"responders": []}).status_code == 404


def test_a_measured_campaign_joins_the_evidence_the_next_decision_sees(make_client):
    client = make_client(ScriptedProvider(PREPAID, PREPAID))
    first = approve(client)
    before = first["measurement"]["evidence_at_approval"]["campaigns"]
    assert client.post(f"/api/audiences/{first['id']}/simulate-outcome").status_code == 200
    evidence = client.get("/api/history/evidence/winback").json()
    assert evidence["campaigns"] == before + 1 and evidence["measured_here"] == 1
    assert any(c["source"] == "approved here (simulated send)" for c in evidence["per_campaign"])
    second = approve(client, name="Second wave")
    assert second["measurement"]["evidence_at_approval"]["campaigns"] == before + 1


def test_the_learning_curve_ends_with_the_campaign_measured_here_and_narrows(make_client):
    client = make_client(ScriptedProvider(PREPAID))
    a = approve(client)
    client.post(f"/api/audiences/{a['id']}/simulate-outcome")
    timeline = client.get("/api/learning/winback").json()["timeline"]
    assert timeline[-1]["audience_id"] == a["id"]
    assert [p["sent_at"] for p in timeline] == sorted(p["sent_at"] for p in timeline)
    width = lambda p: p["estimate_high"] - p["estimate_low"]
    assert width(timeline[-1]) < width(timeline[0])

    summary = client.get("/api/learning").json()
    assert summary["loop"] == {"interpreted": 1, "approved": 1, "approved_with_type": 1, "measured": 1}
    assert summary["scorecard"][0]["audience_id"] == a["id"] and summary["scorecard"][0]["expected"]


def test_simulation_needs_a_campaign_type_and_the_synthetic_history(make_client):
    client = make_client(ScriptedProvider(PREPAID))
    a = approve(client, campaign_type=None)
    assert client.post(f"/api/audiences/{a['id']}/simulate-outcome").status_code == 422
    client.app.state.services.history.synthetic = False
    b = approve(client)
    assert client.post(f"/api/audiences/{b['id']}/simulate-outcome").status_code == 409


def test_a_difference_is_only_claimed_when_the_intervals_do_not_overlap():
    expected = {"lift_points": 1.0, "lift_95_low": 0.8, "lift_95_high": 1.2}
    noisy = counts_lift(2000, 200, 30, 2)  # one small campaign: wide interval that overlaps the expectation
    assert verdict(expected, noisy) == "in line with past campaigns"
    clearly_worse = counts_lift(200_000, 20_000, 2000, 200)  # lift ~0, tight interval
    assert verdict(expected, clearly_worse) == "below what past campaigns predicted"
    clearly_better = counts_lift(200_000, 20_000, 10_000, 200)
    assert verdict(expected, clearly_better) == "above what past campaigns predicted"
    assert verdict(None, noisy) == "no expectation was recorded at approval"
