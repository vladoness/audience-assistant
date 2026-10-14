"""The eval scorer itself: wrong answers must fail, including wrong answers that come back 'invalid'."""

from app.models import AudienceDefinition, Condition, InterpretResult, Issue, Telemetry
from evals.run import score_case

CONTRADICTION = {"id": "c", "category": "x", "request": "Postpaid customers who haven't topped up in 30 days",
                 "expect": {"status_in": ["needs_confirmation", "invalid"],
                            "definition": {"conditions": [{"attribute": "line_type", "op": "eq", "value": "postpaid"}]}}}
POLICY = {"id": "p", "category": "x", "request": "Postpaid customers with a high credit risk band",
          "expect": {"status": "invalid", "issues_include": ["policy_violation"], "forbid_attributes": ["credit_risk_band"]}}


def result(status, conditions, issues):
    return InterpretResult(request_id="r", status=status, issues=issues, telemetry=Telemetry(provider="test"),
                           definition=AudienceDefinition(conditions=[Condition(**c) for c in conditions]))


def err(code, attribute):
    return Issue(level="error", code=code, attribute=attribute, message=code)


def test_the_right_invalid_answer_passes(engine):
    r = result("invalid", [{"attribute": "line_type", "op": "eq", "value": "postpaid"},
                           {"attribute": "days_since_last_recharge", "op": "gt", "value": 30}],
               [err("out_of_scope_condition", "days_since_last_recharge")])
    assert score_case(CONTRADICTION, r, engine)["passed"]


def test_an_invalid_answer_with_a_wrong_audience_fails(engine):
    r = result("invalid", [{"attribute": "region", "op": "eq", "value": "North"},
                           {"attribute": "days_since_last_recharge", "op": "gt", "value": 30}],
               [err("out_of_scope_condition", "days_since_last_recharge")])
    scored = score_case(CONTRADICTION, r, engine)
    assert not scored["passed"] and scored["checks"]["audience"] is False


def test_an_invalid_answer_caused_by_the_model_fails(engine):
    r = result("invalid", [{"attribute": "line_type", "op": "eq", "value": "postpaid"},
                           {"attribute": "favourite_colour", "op": "eq", "value": "red"}],
               [err("unknown_attribute", "favourite_colour")])
    assert score_case(CONTRADICTION, r, engine)["checks"]["only_request_conflicts"] is False
    r = result("invalid", [{"attribute": "credit_risk_band", "op": "eq", "value": "high"},
                           {"attribute": "plan_tier", "op": "eq", "value": "platinum"}],
               [err("policy_violation", "credit_risk_band"), err("invalid_value", "plan_tier")])
    assert not score_case(POLICY, r, engine)["passed"]
