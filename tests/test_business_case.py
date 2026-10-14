"""The business case calculator, and the doc that quotes it."""

import contextlib
import io
import sys

import pytest

from app.settings import ROOT
from scripts.business_case import evaluate, load, main


def test_base_case_arithmetic():
    scenarios, costs = load([])
    base = evaluate(scenarios["base"], costs)
    assert base["value_per_campaign"] == pytest.approx(2000)
    assert base["revenue_speed"] == pytest.approx(60_000)
    assert base["revenue_more_campaigns"] == pytest.approx(96_000)
    assert base["break_even_extra_campaigns_per_month"] == pytest.approx(54_000 / 24_000)
    assert base["payback_months"] == pytest.approx(12 * 50_000 / (base["total"] - 4_000))


def test_an_assumption_can_be_changed_and_unknown_ones_are_refused():
    scenarios, costs = load(["extra_campaigns_per_month=0"])
    assert evaluate(scenarios["base"], costs)["revenue_more_campaigns"] == 0
    with pytest.raises(SystemExit):
        load(["campaign_magic=3"])


def test_the_doc_quotes_the_calculator_exactly(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["business_case"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main()
    doc = (ROOT / "docs" / "BUSINESS-CASE.md").read_text()
    table_rows = [line for line in out.getvalue().splitlines() if line.startswith("| ")]
    assert table_rows and all(row in doc for row in table_rows)
