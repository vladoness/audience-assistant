from app.models import AudienceDefinition, Condition
from app.validate import validate_definition


def check(catalog, *conditions, channel="sms"):
    definition = AudienceDefinition(conditions=[Condition(**c) for c in conditions], channel=channel)
    return validate_definition(definition, catalog)


def codes(issues, level="error"):
    return {i.code for i in issues if i.level == level}


def test_invented_attribute_is_rejected(catalog):
    _, issues = check(catalog, {"attribute": "churn_score", "op": "gt", "value": 0.8})
    assert codes(issues) == {"unknown_attribute"}


def test_credit_data_is_blocked_by_policy_for_every_channel(catalog):
    for channel in ("sms", "email"):
        _, issues = check(catalog, {"attribute": "credit_risk_band", "op": "eq", "value": "high"}, channel=channel)
        assert codes(issues) == {"policy_violation"}
        assert "MKT-07" in issues[0].message


def test_consent_cannot_be_used_as_a_condition(catalog):
    _, issues = check(catalog, {"attribute": "marketing_consent_sms", "op": "eq", "value": False})
    assert codes(issues) == {"policy_violation"}
    assert "MKT-15" in issues[0].message


def test_policies_are_evaluated_per_marketing_action(catalog):
    app_users = {"attribute": "app_active_30d", "op": "eq", "value": True}
    _, sms_issues = check(catalog, app_users, channel="sms")
    _, email_issues = check(catalog, app_users, channel="email")
    assert not codes(sms_issues)
    assert codes(email_issues) == {"policy_violation"}
    assert "MKT-12" in email_issues[0].message


def test_operator_must_fit_the_field_type(catalog):
    _, issues = check(catalog, {"attribute": "region", "op": "gt", "value": "Coast"})
    assert codes(issues) == {"operator_not_allowed"}


def test_enum_values_and_ranges_are_enforced(catalog):
    _, issues = check(catalog, {"attribute": "region", "op": "eq", "value": "Atlantis"},
                      {"attribute": "arpu_3m", "op": "gte", "value": 9000})
    assert codes(issues) == {"invalid_value"}
    assert len(issues) == 2


def test_values_are_coerced_to_the_field_type(catalog):
    definition, issues = check(catalog, {"attribute": "tenure_months", "op": "lt", "value": "3"},
                               {"attribute": "app_active_30d", "op": "eq", "value": "true"})
    assert not issues
    assert [c.value for c in definition.conditions] == [3, True]


def test_fractional_value_for_integer_field_is_rejected(catalog):
    _, issues = check(catalog, {"attribute": "tenure_months", "op": "lt", "value": 2.5})
    assert codes(issues) == {"invalid_value"}


def test_contradictory_numeric_conditions_are_caught(catalog):
    _, issues = check(catalog, {"attribute": "arpu_3m", "op": "gt", "value": 50},
                      {"attribute": "arpu_3m", "op": "lt", "value": 30})
    assert codes(issues) == {"contradictory_conditions"}


def test_integer_gap_with_no_whole_number_is_contradictory(catalog):
    _, issues = check(catalog, {"attribute": "tenure_months", "op": "gt", "value": 5},
                      {"attribute": "tenure_months", "op": "lt", "value": 6})
    assert codes(issues) == {"contradictory_conditions"}


def test_contradictory_enum_conditions_are_caught(catalog):
    _, issues = check(catalog, {"attribute": "line_type", "op": "eq", "value": "prepaid"},
                      {"attribute": "line_type", "op": "eq", "value": "postpaid"})
    assert "contradictory_conditions" in codes(issues)


def test_prepaid_only_field_on_postpaid_audience_is_an_error(catalog):
    _, issues = check(catalog, {"attribute": "line_type", "op": "eq", "value": "postpaid"},
                      {"attribute": "days_since_last_recharge", "op": "gt", "value": 30})
    assert codes(issues) == {"out_of_scope_condition"}


def test_prepaid_only_field_without_line_type_warns_about_implicit_scope(catalog):
    _, issues = check(catalog, {"attribute": "days_since_last_recharge", "op": "gt", "value": 30})
    assert not codes(issues)
    assert codes(issues, "warning") == {"implicit_scope"}


def test_duplicate_conditions_are_collapsed(catalog):
    definition, issues = check(catalog, {"attribute": "roaming_30d", "op": "eq", "value": True},
                               {"attribute": "roaming_30d", "op": "eq", "value": True, "source": "glossary:x"})
    assert not issues
    assert len(definition.conditions) == 1
