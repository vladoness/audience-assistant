"""Measures how sizing latency grows with the profile store, separately from model latency.

    uv run python -m scripts.bench --rows 200000 1000000 5000000

Generates throwaway datasets in a temp dir, then times each definition 20 times (after one
warm-up) and reports p50/p95 per size.
"""

from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from pathlib import Path

from app.audience import AudienceEngine
from app.catalog import load_catalog
from app.models import AudienceDefinition, Condition
from app.settings import ROOT
from app.synthdata import generate

DEFINITIONS = {
    "1 condition": [{"attribute": "line_type", "op": "eq", "value": "prepaid"}],
    "3 conditions": [
        {"attribute": "line_type", "op": "eq", "value": "prepaid"},
        {"attribute": "days_since_last_recharge", "op": "gt", "value": 30},
        {"attribute": "app_active_30d", "op": "eq", "value": True},
    ],
    "5 conditions + in": [
        {"attribute": "region", "op": "in", "value": ["Coast", "Valley", "North"]},
        {"attribute": "arpu_3m", "op": "gte", "value": 20},
        {"attribute": "data_gb_30d", "op": "between", "value": [5, 40]},
        {"attribute": "device_os", "op": "neq", "value": "feature_phone"},
        {"attribute": "tenure_months", "op": "gt", "value": 6},
    ],
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, nargs="+", default=[200_000, 1_000_000, 5_000_000])
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()
    catalog = load_catalog(ROOT / "config")
    tmp = Path(tempfile.mkdtemp())

    print(f"{'rows':>10}  {'generate':>9}  {'definition':<20} {'p50 ms':>8} {'p95 ms':>8}")
    for rows in args.rows:
        path = tmp / f"profiles_{rows}.duckdb"
        gen_s = generate(path, rows)
        engine = AudienceEngine(path, catalog)
        for name, conds in DEFINITIONS.items():
            definition = AudienceDefinition(conditions=[Condition(**c) for c in conds])
            engine.size(definition)
            samples = []
            for _ in range(args.runs):
                started = time.perf_counter()
                engine.size(definition)
                samples.append((time.perf_counter() - started) * 1000)
            samples.sort()
            p95 = samples[max(0, int(len(samples) * 0.95) - 1)]
            print(f"{rows:>10,}  {gen_s:>8.1f}s  {name:<20} {statistics.median(samples):>8.1f} {p95:>8.1f}")


if __name__ == "__main__":
    main()
