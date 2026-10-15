"""Runs an eval set through the real interpreter and reports accuracy, latency and cost.

    uv run python -m evals.run --provider anthropic --model claude-haiku-5-5 --effort low --repeat 3 --record
    uv run python -m evals.run --provider anthropic --model claude-haiku-5-5 --cases heldout
    uv run python -m evals.run --provider anthropic --model claude-haiku-5-5 --cases refine   # changes to an audience
    uv run python -m evals.run --provider replay --model claude-haiku-5-5     # reproduce run 1 offline

A case passes only if every expectation it declares holds. Audience correctness is judged by
outcome: the proposed and expected definitions must select exactly the same reachable lines.
Policy cases check the system guarantee (a forbidden field is never in a sizable definition) and
that the policy is surfaced to the user (issues_include: policy_violation). An "invalid" result
only passes if its errors come from the request itself (a policy, scope or contradiction), never
from the model (an invented field or value), and, when the case gives a definition, if the part
of the audience without errors matches it.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml

from app.audience import AudienceEngine
from app.catalog import load_catalog
from app.history import CampaignHistory
from app.interpret import Interpreter
from app.llm import AnthropicProvider, Pricing, ProviderUnavailable, RecordingProvider, ReplayProvider
from app.models import AudienceDefinition, Condition, InterpretResult, Issue, Telemetry
from app.observability import Metrics, percentile
from app.settings import ROOT, Settings
from app.store import AuditStore, TTLCache
from app.synthdata import generate

SIZABLE = {"ready", "needs_confirmation", "needs_clarification"}
# Errors caused by the request itself (it asks for something not allowed or impossible), as opposed to
# errors the model made (an invented field, a bad value). Only the first kind can make "invalid" right.
REQUEST_CONFLICTS = {"policy_violation", "out_of_scope_condition", "contradictory_conditions"}


def valid_part(result) -> AudienceDefinition:
    """The conditions of an invalid result that carry no error: what it would select once the flagged
    conditions are removed. An invalid answer is only right if this part is right too."""
    flagged = {i.attribute for i in result.issues if i.level == "error" and i.attribute}
    return result.definition.model_copy(
        update={"conditions": [c for c in result.definition.conditions if c.attribute not in flagged]})


def score_case(case: dict, result, engine: AudienceEngine) -> dict:
    expect = case["expect"]
    checks: dict[str, bool] = {}
    accepted = expect.get("status_in") or [expect["status"]]
    checks["status"] = result.status in accepted
    if result.status == "invalid":
        checks["only_request_conflicts"] = {i.code for i in result.issues if i.level == "error"} <= REQUEST_CONFLICTS

    if "definition" in expect and result.status in SIZABLE | {"invalid"}:
        expected_def = AudienceDefinition(
            channel=expect["definition"].get("channel", "sms"),
            conditions=[Condition(**c) for c in expect["definition"]["conditions"]],
        )
        want = engine.profile_ids(expected_def)
        got = engine.profile_ids(result.definition if result.status in SIZABLE else valid_part(result))
        union = want | got
        checks["audience"] = want == got
        jaccard = len(want & got) / len(union) if union else 1.0
    else:
        jaccard = None

    if "clarify" in expect:
        checks["clarify"] = set(expect["clarify"]) <= {c.term for c in result.clarifications}
    if "clarify_negated" in expect:
        checks["clarify_negated"] = set(expect["clarify_negated"]) == {c.term for c in result.clarifications if c.negated}
    if "unmapped_contains" in expect and result.status != "invalid":
        phrases = " | ".join(result.unmapped_phrases).lower()
        checks["unmapped"] = all(s.lower() in phrases for s in expect["unmapped_contains"])
    if "forbid_attributes" in expect:
        used = {c.attribute for c in result.definition.conditions}
        checks["policy_enforced"] = result.status == "invalid" or not (used & set(expect["forbid_attributes"]))
    if "issues_include" in expect:
        checks["issues"] = set(expect["issues_include"]) <= {i.code for i in result.issues}

    t = result.telemetry
    return {
        "id": case["id"],
        "category": case["category"],
        "request": case["request"],
        "passed": all(checks.values()),
        "checks": checks,
        "status": result.status,
        "jaccard": None if jaccard is None else round(jaccard, 4),
        "conditions": [c.model_dump() for c in result.definition.conditions],
        "clarifications": [c.term for c in result.clarifications],
        "unmapped": result.unmapped_phrases,
        "issues": [i.code for i in result.issues],
        "llm_calls": t.llm_calls,
        "repaired": t.repaired,
        "llm_ms": t.llm_ms,
        "tokens_in": t.input_tokens + t.cache_read_tokens + t.cache_write_tokens,
        "tokens_out": t.output_tokens,
        "cost_usd": t.cost_usd,
    }


def run_refine_case(interpreter: Interpreter, case: dict):
    """A refinement case starts from a written-out audience, so it measures the refinement alone."""
    base = AudienceDefinition(**case["base"])
    parent = {"request_id": f"base-{case['id']}", "definition_json": base.model_dump_json(),
              "clarifications_json": json.dumps(case.get("pending", [])),
              "unmapped_json": json.dumps(case.get("unmapped", []))}
    try:
        return interpreter.refine(parent, base, case["request"])
    except ProviderUnavailable as exc:
        return InterpretResult(request_id="-", status="ai_unavailable", definition=base,
                               issues=[Issue(level="warning", code=exc.code, message=str(exc))],
                               telemetry=Telemetry(provider=interpreter.provider.name))


def _matches(p: dict, matcher: dict) -> bool:
    return all(p.get(k) == v for k, v in matcher.items())


def _expectations(expect: dict, proposals: list[dict]) -> dict[str, bool]:
    checks = {"advice": True}
    if "must_include" in expect:
        checks["must_include"] = all(any(_matches(p, m) for p in proposals) for m in expect["must_include"])
    if "must_include_any" in expect:
        checks["must_include_any"] = any(_matches(p, m) for m in expect["must_include_any"] for p in proposals)
    if "must_not_include" in expect:
        checks["must_not_include"] = not any(_matches(p, m) for m in expect["must_not_include"] for p in proposals)
    if "cites_any" in expect:
        checks["cites_any"] = any(str(c).startswith(prefix) for p in proposals for c in p.get("cites", [])
                                  for prefix in expect["cites_any"])
    return checks


def baseline_proposals(name: str, facts: dict[str, dict], definition: AudienceDefinition,
                       campaign_type: str | None, goal: str = "") -> list[dict]:
    """No model. "keep": always "keep the audience as it is". "rule": a few lines of code that narrow on
    every clearly-better segment of the chosen type, mention heavy recent contact, and otherwise cite the
    type's past results; with no type chosen, pick the best-measured type on the audience's channel.
    "keyword": the rule, but with no type chosen it first looks for a type's keywords in the goal."""
    blank = {"title": "", "rationale": "", "attribute": "", "value": "", "channel": "none", "campaign_type": ""}
    if name == "keep":
        return [blank | {"kind": "advice", "title": "Keep the audience as it is", "cites": ["audience"]}]
    out = []
    for fid, f in facts.items():
        d = f["data"]
        if fid.startswith(f"segment.{campaign_type}.") and d["clearly_different"] and d["difference"] > 0:
            out.append(blank | {"kind": "narrow_segment", "cites": [fid], "attribute": d["attribute"], "value": d["value"]})
    if facts.get("pressure.recent", {}).get("data", {}).get("count"):
        out.append(blank | {"kind": "advice", "cites": ["pressure.recent"]})
    if campaign_type:
        out.append(blank | {"kind": "advice", "cites": [f"type.{campaign_type}"]})
    else:
        types = [(f["data"]["low"], fid) for fid, f in facts.items()
                 if fid.startswith("type.") and f["data"]["channel"] == definition.channel]
        guess = next((t for t, words in KEYWORDS.items() if name == "keyword" and re.search(words, goal.lower())), None)
        if guess:
            out.append(blank | {"kind": "choose_campaign_type", "cites": [f"type.{guess}"], "campaign_type": guess})
        elif types:
            fid = max(types)[1]
            out.append(blank | {"kind": "choose_campaign_type", "cites": [fid], "campaign_type": fid.removeprefix("type.")})
    return out[:3]


# The keyword baseline reads the goal with a word list instead of a model.
KEYWORDS = {"roaming_offer": r"roam|travel|abroad|trip", "data_upsell": r"data|bundle|\bgb\b",
            "topup_reminder": r"top.?up|recharge", "winback": r"win.?back|lapsed|come back", "app_adoption": r"\bapp\b"}


def run_advisor_case(interpreter: Interpreter, engine: AudienceEngine, history, store, catalog, case: dict) -> dict:
    """One advisor case: facts from code, proposals from the model, checks from code. Scored on what is
    accepted (what the marketer sees), and also on the raw proposals (what they'd see without the checks)
    and on two baselines that use no model, so the score says what the model adds."""
    from app import advisor

    definition = AudienceDefinition(**case["definition"])
    campaign_type = case.get("campaign_type")
    facts = advisor.fact_sheet(engine, catalog, history, store, definition, campaign_type)
    by_id = {f["id"]: f for f in facts}
    expect = case["expect"]
    baselines = {}
    for name in ("keep", "rule", "keyword"):
        proposals = [p for p in baseline_proposals(name, by_id, definition, campaign_type, case["goal"])
                     if not advisor.check(p, by_id, catalog, definition, campaign_type)]
        baselines[name] = all(_expectations(expect, proposals).values())
    try:
        raw, tel = interpreter.advise(facts, case["goal"], campaign_type)
    except ProviderUnavailable as exc:
        return {"id": case["id"], "category": case["category"], "passed": False, "raw_passed": False,
                "baselines": baselines, "checks": {"advice": False}, "accepted": [], "rejected": [str(exc)],
                "llm_calls": 0, "llm_ms": 0.0, "cost_usd": 0.0}
    proposals = (raw.get("proposals") or [])[:3]
    accepted, rejected = [], []
    for p in proposals:
        reason = advisor.check(p, by_id, catalog, definition, campaign_type)
        (rejected if reason else accepted).append(p if not reason else {"title": p.get("title"), "reason": reason})
    checks = _expectations(expect, accepted)
    return {"id": case["id"], "category": case["category"], "passed": all(checks.values()), "checks": checks,
            "raw_passed": all(_expectations(expect, proposals).values()), "baselines": baselines,
            "accepted": [{k: p.get(k) for k in ("kind", "title", "attribute", "value", "campaign_type", "cites")}
                         for p in accepted],
            "rejected": rejected, "llm_calls": tel.llm_calls, "llm_ms": tel.llm_ms, "cost_usd": tel.cost_usd}


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """95% interval for a pass rate of k out of n cases: how far a small set can be trusted."""
    if not n:
        return None
    p, d = k / n, 1 + z * z / n
    centre, half = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(centre - half, 3), round(centre + half, 3)]


def _case_interval(rows: list[dict]) -> list[float] | None:
    """Over cases (a case passes when every run passed), not case-runs: runs of one case aren't independent."""
    by_case: dict[str, bool] = {}
    for r in rows:
        by_case[r["id"]] = by_case.get(r["id"], True) and r["passed"]
    return wilson(sum(by_case.values()), len(by_case))


def summarize_advice(model: str, rows: list[dict], runs: int) -> dict:
    by_case: dict[str, list[bool]] = defaultdict(list)
    by_cat: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_case[r["id"]].append(r["passed"])
        by_cat[r["category"]].append(r["passed"])
    latencies = sorted(r["llm_ms"] for r in rows if r["llm_calls"])
    rate = lambda xs: round(sum(xs) / len(xs), 3)
    first_run = [r for r in rows if r["run"] == 1]
    return {
        "model": model, "cases": len(by_case), "runs": runs,
        "pass_rate": rate([r["passed"] for r in rows]),
        "cases_passed_every_run_95_interval": _case_interval(rows),
        "raw_pass_rate_without_checks": rate([r["raw_passed"] for r in rows]),
        "baseline_keep_as_is_pass_rate": rate([r["baselines"]["keep"] for r in first_run]),
        "baseline_rule_pass_rate": rate([r["baselines"]["rule"] for r in first_run]),
        "baseline_keyword_rule_pass_rate": rate([r["baselines"]["keyword"] for r in first_run]),
        "proposals_accepted": sum(len(r["accepted"]) for r in rows),
        "proposals_withheld_by_checks": sum(len(r["rejected"]) for r in rows),
        "latency_p50_ms": round(percentile(latencies, 50), 0) if latencies else None,
        "latency_p95_ms": round(percentile(latencies, 95), 0) if latencies else None,
        "cost_per_1k_requests_usd": round(1000 * statistics.mean(r["cost_usd"] for r in rows), 2),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_cat.items())},
        "failures": [{"id": c, "failed_runs": f"{len(v) - sum(v)}/{len(v)}"} for c, v in sorted(by_case.items()) if not all(v)],
        "baseline_failures": {name: sorted(r["id"] for r in first_run if not r["baselines"][name])
                              for name in ("keep", "rule", "keyword")},
    }


def summarize(model: str, rows: list[dict], runs: int) -> dict:
    """Aggregate over every (case, run). With runs > 1, flaky cases are those that passed in some runs only."""
    by_cat: dict[str, list[bool]] = defaultdict(list)
    by_case: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r["passed"])
        by_case[r["id"]].append(r["passed"])
    latencies = sorted(r["llm_ms"] for r in rows if r["llm_calls"])
    audience_rows = [r for r in rows if "audience" in r["checks"]]
    per_run = [round(sum(r["passed"] for r in rows if r["run"] == k) / max(1, sum(1 for r in rows if r["run"] == k)), 3)
               for k in range(1, runs + 1)]
    return {
        "model": model,
        "cases": len(by_case),
        "runs": runs,
        "pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 3),
        "pass_rate_per_run": per_run,
        "cases_passed_every_run_95_interval": _case_interval(rows),
        "status_accuracy": round(sum(r["checks"]["status"] for r in rows) / len(rows), 3),
        "audience_exact_match": round(sum(r["checks"]["audience"] for r in audience_rows) / max(len(audience_rows), 1), 3),
        "repair_rate": round(sum(r["repaired"] for r in rows) / len(rows), 3),
        "latency_p50_ms": round(percentile(latencies, 50), 0) if latencies else None,
        "latency_p95_ms": round(percentile(latencies, 95), 0) if latencies else None,
        "latency_max_ms": round(latencies[-1], 0) if latencies else None,
        "avg_tokens_in": round(statistics.mean(r["tokens_in"] for r in rows), 0),
        "avg_tokens_out": round(statistics.mean(r["tokens_out"] for r in rows), 0),
        "cost_per_1k_requests_usd": round(1000 * statistics.mean(r["cost_usd"] for r in rows), 2),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_cat.items())},
        "flaky_cases": sorted(c for c, v in by_case.items() if 0 < sum(v) < len(v)),
        "failures": [{"id": c, "failed_runs": f"{len(v) - sum(v)}/{len(v)}"} for c, v in sorted(by_case.items()) if not all(v)],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["anthropic", "replay"], default="replay")
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--effort", default="low", help="low | medium | high | xhigh | max | default")
    parser.add_argument("--record", action="store_true", help="save run-1 live responses as replay fixtures")
    parser.add_argument("--cases", default="dev", choices=["dev", "heldout", "refine", "advisor"])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--only", help="run a single case id")
    args = parser.parse_args()

    settings = Settings.from_env()
    catalog = load_catalog(settings.config_dir)
    if not settings.data_path.exists():
        generate(settings.data_path, settings.profile_rows)
    engine = AudienceEngine(settings.data_path, catalog)

    effort = None if args.effort == "default" else args.effort
    live = AnthropicProvider(settings.anthropic_api_key, args.model, settings.llm_timeout_s,
                             settings.llm_max_retries, effort=effort) if args.provider == "anthropic" else None
    pricing = Pricing.load(settings.config_dir / "models.yaml")
    case_file = {"dev": "cases.yaml", "heldout": "heldout.yaml", "refine": "refine.yaml", "advisor": "advisor.yaml"}[args.cases]
    cases = yaml.safe_load((ROOT / "evals" / case_file).read_text())["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
    if args.cases == "advisor":
        # Its own 200k store, generated from the known truth, so results don't depend on a local database.
        store_path = Path(tempfile.mkdtemp()) / "profiles.duckdb"
        generate(store_path, 200_000)
        engine = AudienceEngine(store_path, catalog)
        history = CampaignHistory(engine, catalog)

    rows = []
    for run in range(1, args.repeat + 1):
        if live is None:
            provider = ReplayProvider(settings.fixtures_dir, model=args.model, effort=effort)
        else:
            provider = RecordingProvider(live, settings.fixtures_dir) if (args.record and run == 1) else live
        tmp = Path(tempfile.mkdtemp())
        interpreter = Interpreter(catalog, provider, AuditStore(tmp / "audit.sqlite"), TTLCache(0, 0), Metrics(), pricing,
                                  breaker_threshold=10_000)
        for case in cases:
            if args.cases == "advisor":
                row = run_advisor_case(interpreter, engine, history, AuditStore(tmp / "audit.sqlite"), catalog, case)
                row["run"] = run
                rows.append(row)
                print(f"{'PASS' if row['passed'] else 'FAIL'}  run {run}  {row['id']:<28} {row['llm_ms']:>7.0f} ms  "
                      f"accepted={[p['kind'] for p in row['accepted']]} withheld={len(row['rejected'])}  "
                      f"raw={'pass' if row['raw_passed'] else 'FAIL'} baselines={row['baselines']}  {row['checks']}")
                continue
            result = run_refine_case(interpreter, case) if args.cases == "refine" else interpreter.interpret(case["request"])
            row = score_case(case, result, engine) | {"run": run}
            rows.append(row)
            mark = "PASS" if row["passed"] else "FAIL"
            print(f"{mark}  run {run}  {row['id']:<18} {row['status']:<20} {row['llm_ms']:>7.0f} ms  {row['checks']}")

    summarize_fn = summarize_advice if args.cases == "advisor" else summarize
    summary = summarize_fn(args.model, rows, args.repeat) | {"effort": args.effort, "case_set": args.cases}
    out_dir = ROOT / "evals" / "results"
    out_dir.mkdir(exist_ok=True)
    suffix = "" if args.provider == "anthropic" else ".replay"
    (out_dir / f"{args.model}@{args.effort}.{args.cases}{suffix}.json").write_text(
        json.dumps({"summary": summary, "cases": rows}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
