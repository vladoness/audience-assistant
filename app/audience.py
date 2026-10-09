"""Compiles a validated definition to parameterized SQL and sizes it against the profile store.

The model never writes SQL. Column names come only from the catalog (already validated), all
values are bound parameters, and the consent filter for the chosen channel is added here,
so no request, edit or prompt injection can produce an audience that ignores consent.
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb

from app.catalog import Catalog
from app.models import AudienceDefinition, Issue, SizeResult

_SQL_OPS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


class GrainError(RuntimeError):
    """The profile table is not one row per line, so every count would be wrong."""


class AudienceEngine:
    def __init__(self, data_path: Path, catalog: Catalog, min_audience: int = 100, broad_share: float = 0.5):
        self._con = duckdb.connect(str(data_path), read_only=True)
        self._catalog = catalog
        self._min_audience = min_audience
        self._broad_share = broad_share
        rows, distinct = self._con.execute("SELECT count(*), count(DISTINCT profile_id) FROM profiles").fetchone()
        if rows != distinct:
            raise GrainError(f"profiles has {rows} rows but {distinct} distinct profile_id values")
        missing = set(catalog.attributes) - {r[0] for r in self._con.execute("DESCRIBE profiles").fetchall()}
        if missing:
            raise RuntimeError(f"profiles table is missing catalog columns: {sorted(missing)}")
        self.base = rows

    def compile(self, definition: AudienceDefinition) -> tuple[str, list]:
        """Return (WHERE clause, params) for the targeting conditions (consent excluded)."""
        clauses, params = [], []
        for cond in definition.conditions:
            if cond.attribute not in self._catalog.attributes:
                raise ValueError(f"refusing to compile unknown attribute {cond.attribute!r}")
            column = f'"{cond.attribute}"'
            if cond.op in _SQL_OPS:
                clauses.append(f"{column} {_SQL_OPS[cond.op]} ?")
                params.append(cond.value)
            elif cond.op in {"in", "not_in"}:
                placeholders = ", ".join("?" for _ in cond.value)
                clauses.append(f"{column} {'IN' if cond.op == 'in' else 'NOT IN'} ({placeholders})")
                params.extend(cond.value)
            elif cond.op == "between":
                clauses.append(f"{column} BETWEEN ? AND ?")
                params.extend(cond.value)
        return (" AND ".join(clauses) or "TRUE"), params

    def size(self, definition: AudienceDefinition) -> SizeResult:
        where, params = self.compile(definition)
        consent = f'"{self._catalog.channels[definition.channel]}"'
        started = time.perf_counter()
        cur = self._con.cursor()
        matched, reachable = cur.execute(
            f"SELECT count(*), count(*) FILTER (WHERE {consent}) FROM profiles WHERE {where}", params
        ).fetchone()
        breakdown = cur.execute(
            f"SELECT line_type, region, count(*) FROM profiles WHERE {where} AND {consent} GROUP BY ALL", params
        ).fetchall()
        query_ms = (time.perf_counter() - started) * 1000

        by_line_type: dict[str, int] = {}
        by_region: dict[str, int] = {}
        for line_type, region, n in breakdown:
            by_line_type[line_type] = by_line_type.get(line_type, 0) + n
            by_region[region] = by_region.get(region, 0) + n

        return SizeResult(
            base=self.base,
            matched=matched,
            reachable=reachable,
            excluded_no_consent=matched - reachable,
            share_of_base=round(reachable / self.base, 4) if self.base else 0.0,
            by_line_type=dict(sorted(by_line_type.items())),
            by_region=dict(sorted(by_region.items(), key=lambda kv: -kv[1])),
            warnings=self._guardrails(matched, reachable),
            query_ms=round(query_ms, 2),
        )

    def profile_ids(self, definition: AudienceDefinition, reachable_only: bool = True) -> set[str]:
        """Exact membership, used by the evals to compare two definitions by the audience they produce."""
        where, params = self.compile(definition)
        if reachable_only:
            where += f' AND "{self._catalog.channels[definition.channel]}"'
        rows = self._con.cursor().execute(f"SELECT profile_id FROM profiles WHERE {where}", params).fetchall()
        return {r[0] for r in rows}

    def _guardrails(self, matched: int, reachable: int) -> list[Issue]:
        warnings = []
        if matched == 0:
            warnings.append(Issue(level="warning", code="empty_audience", message="No lines match these conditions."))
        elif reachable < self._min_audience:
            warnings.append(Issue(
                level="warning", code="audience_too_small",
                message=f"Only {reachable} reachable lines (minimum {self._min_audience}): results won't be measurable.",
            ))
        if self.base and reachable / self.base > self._broad_share:
            warnings.append(Issue(
                level="warning", code="audience_very_broad",
                message=f"Audience is {reachable / self.base:.0%} of all lines: check the conditions are as intended.",
            ))
        return warnings
