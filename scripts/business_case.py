"""Business case calculator for docs/BUSINESS-CASE.md: low / base / high, payback, break-even and
which assumptions move the answer most.

    uv run python -m scripts.business_case
    uv run python -m scripts.business_case --set incremental_response_rate=0.005 --set extra_campaigns_per_month=2
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

ASSUMPTIONS = Path(__file__).resolve().parents[1] / "docs" / "business_case.yaml"

LABELS = {
    "time_sensitive_share": "Share of campaigns that are time-sensitive",
    "value_lost_to_delay": "Value a late campaign loses",
    "incremental_response_rate": "Extra response rate vs holdout",
    "margin_per_response_usd": "Margin per responder",
    "extra_campaigns_per_month": "Extra campaigns per month",
    "expected_penalties_avoided_usd": "Expected consent penalties avoided",
    "analyst_hours_per_ticket": "Analyst hours per ticket",
    "rework_hours_per_disputed_ticket": "Rework hours per disputed ticket",
    "analyst_cost_per_month_usd": "Analyst cost per month",
}


def benefits(a: dict) -> dict[str, float]:
    """Annual benefit per lever, in USD, for one scenario's assumptions (fixed inputs included)."""
    value_per_campaign = a["reachable_lines_per_campaign"] * a["incremental_response_rate"] * a["margin_per_response_usd"]
    tickets = a["campaigns_per_month"]
    analyst_hours = tickets * a["analyst_hours_per_ticket"] \
        + tickets * a["disputed_ticket_share"] * a["rework_hours_per_disputed_ticket"]
    return {
        "value_per_campaign": value_per_campaign,
        "revenue_speed": 12 * tickets * a["time_sensitive_share"] * value_per_campaign * a["value_lost_to_delay"],
        "revenue_more_campaigns": 12 * a["extra_campaigns_per_month"] * value_per_campaign,
        "risk_penalties_avoided": a["expected_penalties_avoided_usd"],
        "productivity": 12 * analyst_hours / a["analyst_hours_per_month_fte"] * a["analyst_cost_per_month_usd"],
        "analyst_hours_per_month": analyst_hours,
    }


def evaluate(a: dict, costs: dict) -> dict[str, float]:
    b = benefits(a)
    total = b["revenue_speed"] + b["revenue_more_campaigns"] + b["risk_penalties_avoided"] + b["productivity"]
    net_running = total - costs["running_per_year_usd"]
    first_year = costs["delivery_one_off_usd"] + costs["running_per_year_usd"]
    return b | {
        "total": total,
        "payback_months": 12 * costs["delivery_one_off_usd"] / net_running if net_running > 0 else float("inf"),
        "productivity_only_payback_months": (
            12 * costs["delivery_one_off_usd"] / (b["productivity"] - costs["running_per_year_usd"])
            if b["productivity"] > costs["running_per_year_usd"] else float("inf")),
        "break_even_extra_campaigns_per_month": first_year / (12 * b["value_per_campaign"]),
    }


def load(overrides: list[str]) -> tuple[dict[str, dict], dict]:
    data = yaml.safe_load(ASSUMPTIONS.read_text())
    scenarios = {name: {**data["fixed"], **values} for name, values in data["scenarios"].items()}
    for item in overrides:
        key, _, value = item.partition("=")
        if key not in scenarios["base"]:
            raise SystemExit(f"unknown assumption {key!r}; choose from: {', '.join(sorted(scenarios['base']))}")
        scenarios["base"][key] = float(value)
    return scenarios, data["costs"]


def sensitivity(scenarios: dict[str, dict], costs: dict) -> list[tuple[str, float, float]]:
    """Total benefit with one assumption at its low and high value, everything else at base."""
    rows = []
    for key in scenarios["base"]:
        lo, hi = scenarios["low"][key], scenarios["high"][key]
        if lo == hi:
            continue
        at = lambda v: evaluate({**scenarios["base"], key: v}, costs)["total"]
        rows.append((key, at(lo), at(hi)))
    return sorted(rows, key=lambda r: -(r[2] - r[1]))


def _usd(v: float) -> str:
    return f"${v / 1000:,.0f}k" if abs(v) >= 1000 else f"${v:,.0f}"


def _months(m: float) -> str:
    if m == float("inf"):
        return "never"
    if m < 1:
        return "under a month"
    return f"~{m:.0f} months" if m < 24 else f"~{m / 12:.1f} years"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="change one assumption in the base case")
    args = parser.parse_args()
    scenarios, costs = load(args.set)
    results = {name: evaluate(a, costs) for name, a in scenarios.items()}
    names = list(results)

    rows = [
        ("Value of one campaign (extra margin)", "value_per_campaign", _usd),
        ("Revenue: time-sensitive campaigns on time", "revenue_speed", _usd),
        ("Revenue: extra campaigns and tests", "revenue_more_campaigns", _usd),
        ("Consent penalties avoided (expected)", "risk_penalties_avoided", _usd),
        ("Analyst time redeployed", "productivity", _usd),
        ("**Benefit per year**", "total", lambda v: f"**{_usd(v)}**"),
        ("Payback on delivery", "payback_months", _months),
        ("Payback from analyst time alone", "productivity_only_payback_months", _months),
        ("Extra campaigns a month that alone cover year 1", "break_even_extra_campaigns_per_month", lambda v: f"{v:.1f}"),
    ]
    print("| | " + " | ".join(n.capitalize() for n in names) + " |")
    print("|---|" + "---|" * len(names))
    for label, key, fmt in rows:
        print(f"| {label} | " + " | ".join(fmt(results[n][key]) for n in names) + " |")
    print(f"\nCosts: {_usd(costs['delivery_one_off_usd'])} delivery once, {_usd(costs['running_per_year_usd'])}/year running."
          f" Analyst hours per month (base): {results['base']['analyst_hours_per_month']:.0f}.")
    if args.set:
        print(f"Base case with: {', '.join(args.set)}")

    print("\nWhat moves the base case most (total benefit with one assumption at low / high):\n")
    print("| Assumption | Low | High |")
    print("|---|---|---|")
    for key, lo, hi in sensitivity(scenarios, costs):
        print(f"| {LABELS.get(key, key)} | {_usd(lo)} | {_usd(hi)} |")


if __name__ == "__main__":
    main()
