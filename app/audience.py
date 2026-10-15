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
        columns = ", ".join(f'"{name}"' for name in sorted(catalog.attributes))
        (digest,) = self._con.execute(f"SELECT bit_xor(hash(profile_id, {columns})) FROM profiles").fetchone()
        self.snapshot = f"{rows}-{digest:016x}"  # changes whenever any profile row changes (e.g. the nightly load)

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        """The read-only store connection, for other read models over the same data (campaign history)."""
        return self._con

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

    def mix(self, definition: AudienceDefinition, attributes: list[str]) -> dict[str, dict[str, int]]:
        """Reachable lines of the audience by value of each attribute (for the advisor's facts)."""
        where, params = self.compile(definition)
        consent = f'"{self._catalog.channels[definition.channel]}"'
        out = {}
        for attr in attributes:
            if attr not in self._catalog.attributes:
                continue
            rows = self._con.cursor().execute(
                f'SELECT "{attr}", count(*) FROM profiles WHERE {where} AND {consent} GROUP BY 1 ORDER BY 2 DESC',
                params).fetchall()
            out[attr] = {str(v): n for v, n in rows if v is not None}
        return out

    def compare(self, definitions: list[AudienceDefinition]) -> list[tuple[int, int]]:
        """For alternative definitions on the same channel: reachable lines of each, and how many of those
        no other alternative reaches. One scan, so a choice can show its consequence before it is made."""
        consent = f'"{self._catalog.channels[definitions[0].channel]}"'
        compiled = [self.compile(d) for d in definitions]
        exprs, params = [], []
        for i, (where, where_params) in enumerate(compiled):
            others = [compiled[j] for j in range(len(compiled)) if j != i]
            # coalesce: a NULL comparison (e.g. a prepaid-only field on a postpaid line) means "not reached".
            any_other = " OR ".join(f"coalesce(({w}), false)" for w, _ in others) or "false"
            exprs += [f"count(*) FILTER (WHERE ({where}))", f"count(*) FILTER (WHERE ({where}) AND NOT ({any_other}))"]
            params += [*where_params, *where_params, *(p for _, ps in others for p in ps)]
        row = self._con.cursor().execute(f"SELECT {', '.join(exprs)} FROM profiles WHERE {consent}", params).fetchone()
        return [(row[2 * i], row[2 * i + 1]) for i in range(len(definitions))]

    def split(self, definition: AudienceDefinition, salt: str, holdout_share: float) -> tuple[int, int]:
        """Reachable lines that get the campaign, and those held out to measure it. Membership is a hash of
        salt and profile id, so the same record always reproduces the same two groups."""
        where, params = self.compile(definition)
        consent = f'"{self._catalog.channels[definition.channel]}"'
        bucket = "md5_number_lower(? || ':' || profile_id) % 10000"
        cutoff = round(holdout_share * 10_000)
        target, holdout = self._con.cursor().execute(
            f"SELECT count(*) FILTER (WHERE {bucket} >= ?), count(*) FILTER (WHERE {bucket} < ?) "
            f"FROM profiles WHERE {where} AND {consent}",
            [salt, cutoff, salt, cutoff, *params],
        ).fetchone()
        return target, holdout

    def _groups_sql(self, definition: AudienceDefinition, salt: str, holdout_share: float) -> tuple[str, str, list]:
        where, params = self.compile(definition)
        consent = f'"{self._catalog.channels[definition.channel]}"'
        bucket = f"md5_number_lower(? || ':' || profile_id) % 10000 < {round(holdout_share * 10_000)}"
        return f"FROM profiles WHERE {where} AND {consent}", bucket, params

    def members(self, definition: AudienceDefinition, salt: str, holdout_share: float) -> list[tuple[str, str]]:
        """Every reachable line with its group, for the hand-off to the campaign tool."""
        source, bucket, params = self._groups_sql(definition, salt, holdout_share)
        return self._con.cursor().execute(
            f"SELECT profile_id, CASE WHEN {bucket} THEN 'held_out' ELSE 'contacted' END {source} ORDER BY profile_id",
            [salt, *params]).fetchall()

    def member_digests(self, definition: AudienceDefinition, salt: str, holdout_share: float) -> dict[str, str]:
        """Digest of exactly which lines are in each group, so a later export can prove it is the approved set."""
        source, bucket, params = self._groups_sql(definition, salt, holdout_share)
        contacted, held_out = self._con.cursor().execute(
            f"SELECT md5(coalesce(string_agg(profile_id, ',' ORDER BY profile_id) FILTER (WHERE NOT ({bucket})), '')), "
            f"md5(coalesce(string_agg(profile_id, ',' ORDER BY profile_id) FILTER (WHERE {bucket}), '')) {source}",
            [salt, salt, *params]).fetchone()
        return {"contacted": contacted[:16], "held_out": held_out[:16]}

    @property
    def min_audience(self) -> int:
        return self._min_audience

    def share_where(self, definition: AudienceDefinition, sql: str) -> float:
        """Share of this audience's reachable lines that also meet a fixed SQL condition (a campaign type's
        eligibility, written in code in CAMPAIGN_TYPES, never from a request)."""
        where, params = self.compile(definition)
        consent = f'"{self._catalog.channels[definition.channel]}"'
        total, inside = self._con.cursor().execute(
            f"SELECT count(*), count(*) FILTER (WHERE coalesce(({sql}), false)) FROM profiles WHERE {where} AND {consent}",
            params).fetchone()
        return inside / total if total else 0.0

    def without_consent(self, profile_ids: list[str], channel: str) -> set[str]:
        """Of these lines, the ones that can no longer be contacted on the channel: consent withdrawn since,
        or gone from the profile store. Checked at every hand-off, because membership is frozen at approval."""
        if not profile_ids:
            return set()
        consent = f'"{self._catalog.channels[channel]}"'
        rows = self._con.cursor().execute(  # one joined string: binding a long list parameter is ~200x slower
            f"SELECT m.pid FROM (SELECT unnest(string_split(?, chr(31))) AS pid) m "
            f"LEFT JOIN profiles p ON p.profile_id = m.pid WHERE p.profile_id IS NULL OR NOT coalesce(p.{consent}, false)",
            ["\x1f".join(profile_ids)]).fetchall()
        return {r[0] for r in rows}

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
