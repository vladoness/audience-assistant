from app.explain import explain
from app.models import AudienceDefinition, Condition
from app.pii import mask_pii


def test_explanation_is_generated_from_the_definition_and_names_governed_decisions(catalog):
    d = AudienceDefinition(channel="email", conditions=[
        Condition(attribute="line_type", op="eq", value="postpaid"),
        Condition(attribute="arpu_3m", op="gte", value=40, source="glossary:high_value"),
        Condition(attribute="region", op="in", value=["Coast", "Valley"]),
    ])
    e = explain(d, catalog)
    assert e.summary == (
        "Lines where line type is postpaid, average monthly revenue (last 3 months) is at least 40 USD/month "
        "and region is Coast or Valley. Only lines with Email marketing consent are included "
        "(consent policy for email_marketing, applied automatically)."
    )
    assert "decision D-014" in e.lines[1] and "parameters v3 from 2026-08-12" in e.lines[1]
    assert e.governed_terms == ["high_value"]


def test_explanation_is_deterministic(catalog):
    d = AudienceDefinition(conditions=[Condition(attribute="roaming_30d", op="eq", value=True)])
    assert explain(d, catalog) == explain(d, catalog)


def test_pii_is_masked_but_ordinary_numbers_are_kept():
    text, n = mask_pii("Like ana.perez@mail.com or +591 7123 4567, line L00012345, over 30 days and 10 GB")
    assert n == 3
    assert "[EMAIL]" in text and "[PHONE]" in text and "[ID]" in text
    assert "30 days" in text and "10 GB" in text
