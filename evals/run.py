"""Runs an eval set through the real interpreter and reports accuracy, latency and cost.

    uv run python -m evals.run --provider anthropic --model claude-haiku-5-5 --effort low --repeat 3 --record
    uv run python -m evals.run --provider anthropic --model claude-haiku-5-5 --cases heldout
    uv run python -m evals.run --provider replay --model claude-haiku-5-5     # reproduce run 1 offline

A case passes only if every expectation it declares holds. Audience correctness is judged by
outcome: the proposed and expected definitions must select exactly the same reachable lines.
Policy cases check the system guarantee (a forbidden field is never in a sizable definition) and
that the policy is surfaced to the user (issues_include: policy_violation).
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml

from app.audience import AudienceEngine
from app.catalog import load_catalog
from app.interpret import Interpreter
from app.llm import AnthropicProvider, Pricing, RecordingProvider, ReplayProvider
from app.models import AudienceDefinition, Condition
from app.observability import Metrics, percentile
from app.settings import ROOT, Settings
from app.store import AuditStore, TTLCache
from app.synthdata import generate

SIZABLE = {"ready", "needs_confirmation", "needs_clarification"}


def score_case(case: dict, result, engine: AudienceEngine) -> dict:
    expect = case["expect"]
    checks: dict[str, bool] = {}
    accepted = expect.get("status_in") or [expect["status"]]
    checks["status"] = result.status in accepted

    if "definition" in expect and result.status in SIZABLE:
        expected_def = AudienceDefinition(
            channel=expect["definition"].get("channel", "sms"),
            conditions=[Condition(**c) for c in expect["definition"]["conditions"]],
        )
        want = engine.profile_ids(expected_def)
        got = engine.profile_ids(result.definition)
        union = want | got
        checks["audience"] = want == got
        jaccard = len(want & got) / len(union) if union else 1.0
    else:
        jaccard = None

    if "clarify" in expect:
        checks["clarify"] = set(expect["clarify"]) <= {c.term for c in result.clarifications}
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
    parser.add_argument("--cases", default="dev", choices=["dev", "heldout"])
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
    case_file = "cases.yaml" if args.cases == "dev" else "heldout.yaml"
    cases = yaml.safe_load((ROOT / "evals" / case_file).read_text())["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]

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
            result = interpreter.interpret(case["request"])
            row = score_case(case, result, engine) | {"run": run}
            rows.append(row)
            mark = "PASS" if row["passed"] else "FAIL"
            print(f"{mark}  run {run}  {row['id']:<18} {row['status']:<20} {row['llm_ms']:>7.0f} ms  {row['checks']}")

    summary = summarize(args.model, rows, args.repeat) | {"effort": args.effort, "case_set": args.cases}
    out_dir = ROOT / "evals" / "results"
    out_dir.mkdir(exist_ok=True)
    suffix = "" if args.provider == "anthropic" else ".replay"
    (out_dir / f"{args.model}@{args.effort}.{args.cases}{suffix}.json").write_text(
        json.dumps({"summary": summary, "cases": rows}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
