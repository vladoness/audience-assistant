import duckdb
import pytest

from app.audience import AudienceEngine, GrainError
from app.models import AudienceDefinition, Condition


def definition(*conditions, channel="sms"):
    return AudienceDefinition(conditions=[Condition(**c) for c in conditions], channel=channel)


def test_size_matches_an_independent_query_and_applies_consent(engine, data_path):
    d = definition({"attribute": "line_type", "op": "eq", "value": "prepaid"},
                   {"attribute": "region", "op": "in", "value": ["Coast", "Valley"]})
    result = engine.size(d)
    with duckdb.connect(str(data_path), read_only=True) as con:
        matched, sms = con.execute(
            "SELECT count(*), sum(marketing_consent_sms::int) FROM profiles "
            "WHERE line_type = 'prepaid' AND region IN ('Coast', 'Valley')").fetchone()
    assert result.matched == matched
    assert result.reachable == sms
    assert result.excluded_no_consent == matched - sms
    assert sum(result.by_line_type.values()) == result.reachable == sum(result.by_region.values())


def test_channel_changes_which_consent_is_applied(engine):
    base = [{"attribute": "line_type", "op": "eq", "value": "postpaid"}]
    sms = engine.size(definition(*base, channel="sms"))
    email = engine.size(definition(*base, channel="email"))
    assert sms.matched == email.matched
    assert sms.reachable != email.reachable


def test_values_are_bound_parameters_never_interpolated(engine):
    hostile = "Coast' OR 1=1 --"
    where, params = engine.compile(definition({"attribute": "region", "op": "eq", "value": hostile}))
    assert hostile not in where
    assert params == [hostile]


def test_unknown_attribute_is_refused_by_the_compiler(engine):
    with pytest.raises(ValueError):
        engine.compile(definition({"attribute": "1=1; DROP TABLE profiles", "op": "eq", "value": 1}))


def test_guardrails_flag_empty_and_overly_broad_audiences(engine):
    empty = engine.size(definition({"attribute": "usage_events_30d", "op": "gte", "value": 19999}))
    assert {w.code for w in empty.warnings} == {"empty_audience"}
    everyone = engine.size(definition())
    assert "audience_very_broad" in {w.code for w in everyone.warnings}


def test_engine_refuses_data_with_duplicate_profiles(tmp_path, catalog, data_path):
    broken = tmp_path / "dupes.duckdb"
    with duckdb.connect(str(broken)) as con:
        con.execute(f"ATTACH '{data_path}' AS src (READ_ONLY)")
        con.execute("CREATE TABLE profiles AS SELECT * FROM src.profiles UNION ALL SELECT * FROM src.profiles LIMIT 50000")
    with pytest.raises(GrainError):
        AudienceEngine(broken, catalog)
