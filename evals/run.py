"""Runs the golden set through the real interpreter and reports accuracy, latency and cost.

    uv run python -m evals.run --provider anthropic --model claude-haiku-4-5 --record
    uv run python -m evals.run --provider replay --model claude-haiku-4-5     # reproduce offline from fixtures

A case passes only if every expectation it declares holds. Audience correctness is judged by
outcome: the proposed and expected definitions must select exactly the same reachable lines.
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
from app.observability import Metrics
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
        checks["forbidden"] = not (used & set(expect["forbid_attributes"]))

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


def summarize(model: str, rows: list[dict]) -> dict:
    by_cat: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r["passed"])
    latencies = sorted(r["llm_ms"] for r in rows if r["llm_calls"])
    audience_rows = [r for r in rows if "audience" in r["checks"]]
    return {
        "model": model,
        "cases": len(rows),
        "pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 3),
        "status_accuracy": round(sum(r["checks"]["status"] for r in rows) / len(rows), 3),
        "audience_exact_match": round(sum(r["checks"]["audience"] for r in audience_rows) / max(len(audience_rows), 1), 3),
        "repair_rate": round(sum(r["repaired"] for r in rows) / len(rows), 3),
        "latency_p50_ms": round(statistics.median(latencies), 0) if latencies else None,
        "latency_p95_ms": round(latencies[max(0, int(len(latencies) * 0.95) - 1)], 0) if latencies else None,
        "avg_tokens_in": round(statistics.mean(r["tokens_in"] for r in rows), 0),
        "avg_tokens_out": round(statistics.mean(r["tokens_out"] for r in rows), 0),
        "cost_per_1k_requests_usd": round(1000 * statistics.mean(r["cost_usd"] for r in rows), 2),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_cat.items())},
        "failures": [{"id": r["id"], "status": r["status"], "checks": r["checks"]} for r in rows if not r["passed"]],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["anthropic", "replay"], default="replay")
    parser.add_argument("--model", default="claude-haiku-4-5")
    parser.add_argument("--record", action="store_true", help="save live responses as replay fixtures")
    parser.add_argument("--only", help="run a single case id")
    args = parser.parse_args()

    settings = Settings.from_env()
    catalog = load_catalog(settings.config_dir)
    if not settings.data_path.exists():
        generate(settings.data_path, settings.profile_rows)
    engine = AudienceEngine(settings.data_path, catalog)

    if args.provider == "anthropic":
        provider = AnthropicProvider(settings.anthropic_api_key, args.model, settings.llm_timeout_s, settings.llm_max_retries)
        if args.record:
            provider = RecordingProvider(provider, settings.fixtures_dir)
    else:
        provider = ReplayProvider(settings.fixtures_dir, model=args.model)

    tmp = Path(tempfile.mkdtemp())
    interpreter = Interpreter(catalog, provider, AuditStore(tmp / "audit.sqlite"), TTLCache(0, 0), Metrics(),
                              Pricing.load(settings.config_dir / "models.yaml"))

    cases = yaml.safe_load((ROOT / "evals" / "cases.yaml").read_text())["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
    rows = []
    for case in cases:
        result = interpreter.interpret(case["request"])
        row = score_case(case, result, engine)
        rows.append(row)
        mark = "PASS" if row["passed"] else "FAIL"
        print(f"{mark}  {row['id']:<18} {row['status']:<20} {row['llm_ms']:>7.0f} ms  {row['checks']}")

    summary = summarize(args.model, rows)
    out_dir = ROOT / "evals" / "results"
    out_dir.mkdir(exist_ok=True)
    suffix = "" if args.provider == "anthropic" else ".replay"
    (out_dir / f"{args.model}{suffix}.json").write_text(json.dumps({"summary": summary, "cases": rows}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
