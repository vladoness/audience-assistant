"""Persistence: the audit trail (SQLite) and the interpretation cache (in memory).

Every interpretation is recorded with the masked request, the raw model proposal, the model
and prompt version, and the versions of every governance file. A refinement is its own record,
linked to the interpretation it refined (parent_request_id). An approval references that
record and adds a governance snapshot: which governed terms it used, under which decision and
parameter versions. That is what lets the system tell, later, which approved audiences a
glossary or parameter change affects. It also records how the decision will be judged: the
campaign goal and the holdout group that measures it, and the exact member list, so the export and
the outcome count never depend on a later data refresh. Outcomes are pending until a person
confirms them; every advice response is kept with the facts it was given.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 11

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interpretations (
    request_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    request_text TEXT NOT NULL,
    status TEXT NOT NULL,
    proposal_json TEXT,
    definition_json TEXT NOT NULL,
    unmapped_json TEXT NOT NULL,
    clarifications_json TEXT NOT NULL,
    issues_json TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT,
    governance_versions TEXT NOT NULL,
    telemetry_json TEXT NOT NULL,
    parent_request_id TEXT REFERENCES interpretations(request_id)
);
CREATE TABLE IF NOT EXISTS audiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL,
    approver TEXT NOT NULL,
    request_id TEXT NOT NULL REFERENCES interpretations(request_id),
    definition_json TEXT NOT NULL,
    definition_hash TEXT NOT NULL,
    edited_by_user INTEGER NOT NULL,
    explanation TEXT NOT NULL,
    channel TEXT NOT NULL,
    matched INTEGER NOT NULL,
    reachable INTEGER NOT NULL,
    acknowledged_unmapped_json TEXT NOT NULL,
    governance_json TEXT NOT NULL,
    governance_versions TEXT NOT NULL,
    measurement_json TEXT NOT NULL,
    client_json TEXT NOT NULL,
    advice_id INTEGER REFERENCES advice(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS audiences_one_per_request ON audiences(request_id);
CREATE TABLE IF NOT EXISTS audience_members (
    audience_id INTEGER NOT NULL REFERENCES audiences(id),
    profile_id TEXT NOT NULL,
    grp TEXT NOT NULL CHECK (grp IN ('contacted', 'held_out')),
    excluded TEXT,  -- why the line was left out after approval (consent withdrawn, gone from the data)
    PRIMARY KEY (audience_id, profile_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS handoffs (  -- when the list was handed to the campaign tool: groups are frozen from then on
    audience_id INTEGER PRIMARY KEY REFERENCES audiences(id),
    handed_off_at TEXT NOT NULL,
    how TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    audience_id INTEGER PRIMARY KEY REFERENCES audiences(id),
    recorded_at TEXT NOT NULL,
    source TEXT NOT NULL,
    campaign_type TEXT,
    sent_at TEXT NOT NULL,
    contacted INTEGER NOT NULL,
    contacted_responders INTEGER NOT NULL,
    held_out INTEGER NOT NULL,
    held_out_responders INTEGER NOT NULL,
    unknown_responders INTEGER NOT NULL,
    membership_matches INTEGER NOT NULL,
    reporter_json TEXT NOT NULL,
    reports INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed')),
    confirmed_by TEXT,
    confirmed_at TEXT,
    confirmer_json TEXT
);
CREATE TABLE IF NOT EXISTS outcome_reports (  -- every report as received, including the ones a corrected file replaced
    audience_id INTEGER NOT NULL REFERENCES audiences(id),
    recorded_at TEXT NOT NULL,
    source TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    contacted INTEGER NOT NULL,
    contacted_responders INTEGER NOT NULL,
    held_out INTEGER NOT NULL,
    held_out_responders INTEGER NOT NULL,
    unknown_responders INTEGER NOT NULL,
    reporter_json TEXT NOT NULL,
    accepted INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS advice (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    request_id TEXT,  -- the interpretation the advice was given for: its "advisor" labels are valid only there
    campaign_type TEXT,
    goal_masked TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    accepted_json TEXT NOT NULL,
    rejected_json TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT
);
"""

_OUTCOME_COLUMNS = ("source", "campaign_type", "sent_at", "contacted", "contacted_responders", "held_out",
                    "held_out_responders", "unknown_responders", "membership_matches", "reporter_json")


def definition_hash(definition: dict[str, Any]) -> str:
    """Stable hash of the targeting logic (ignores where each condition came from)."""
    core = {
        "channel": definition["channel"],
        "conditions": sorted(
            (json.dumps({k: c[k] for k in ("attribute", "op", "value")}, sort_keys=True) for c in definition["conditions"])
        ),
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


class AuditStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        """Older local databases are renamed aside (dev data), never silently reused."""
        current = self._db.execute("PRAGMA user_version").fetchone()[0]
        if current < SCHEMA_VERSION:
            existing = {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("interpretations", "audiences", "audience_members", "handoffs", "outcomes", "outcome_reports",
                          "advice"):
                if table in existing:
                    self._db.execute(f"ALTER TABLE {table} RENAME TO {table}_v{current}_{int(time.time())}")
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._db.commit()

    def record_interpretation(self, row: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO interpretations VALUES (:request_id, :created_at, :request_text, :status, :proposal_json,
                   :definition_json, :unmapped_json, :clarifications_json, :issues_json, :model, :prompt_version,
                   :governance_versions, :telemetry_json, :parent_request_id)""",
                {"created_at": _now(), "parent_request_id": None, **row},
            )

    def get_interpretation(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM interpretations WHERE request_id = ?", (request_id,)).fetchone()
        return dict(row) if row else None

    def approval_for(self, request_id: str) -> int | None:
        with self._lock:
            row = self._db.execute("SELECT id FROM audiences WHERE request_id = ?", (request_id,)).fetchone()
        return row["id"] if row else None

    def can_write(self) -> bool:
        """Readiness: take and release a write lock without changing anything."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute("ROLLBACK")
            return True

    def record_approval(self, row: dict[str, Any], members: list[tuple[str, str]] = ()) -> int:
        """The approval and its exact membership (every line with its group), in one transaction: the export
        and the outcome count read these rows, so neither changes when the profile data is refreshed."""
        with self._lock, self._db:
            cur = self._db.execute(
                """INSERT INTO audiences (created_at, name, approver, request_id, definition_json, definition_hash,
                   edited_by_user, explanation, channel, matched, reachable, acknowledged_unmapped_json,
                   governance_json, governance_versions, measurement_json, client_json, advice_id)
                   VALUES (:created_at, :name, :approver, :request_id, :definition_json, :definition_hash,
                   :edited_by_user, :explanation, :channel, :matched, :reachable, :acknowledged_unmapped_json,
                   :governance_json, :governance_versions, :measurement_json, :client_json, :advice_id)""",
                {"created_at": _now(), "client_json": "{}", "advice_id": None, **row},
            )
            self._db.executemany("INSERT INTO audience_members (audience_id, profile_id, grp) VALUES (?, ?, ?)",
                                 ((cur.lastrowid, pid, grp) for pid, grp in members))
            return cur.lastrowid

    def record_approval_once(self, row: dict[str, Any], members: list[tuple[str, str]] = ()) -> int | None:
        """Insert an approval; None if this interpretation was approved concurrently."""
        try:
            return self.record_approval(row, members)
        except sqlite3.IntegrityError:
            return None

    def members(self, audience_id: int, group: str | None = None) -> list[tuple[str, str]]:
        """The membership recorded at approval, minus lines left out since, optionally one group only."""
        with self._lock:
            rows = self._db.execute(
                "SELECT profile_id, grp FROM audience_members WHERE audience_id = ? AND excluded IS NULL "
                "AND (? IS NULL OR grp = ?) ORDER BY profile_id", (audience_id, group, group)).fetchall()
        return [(r[0], r[1]) for r in rows]

    def handed_off_at(self, audience_id: int) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT handed_off_at FROM handoffs WHERE audience_id = ?", (audience_id,)).fetchone()
        return row[0] if row else None

    def record_handoff(self, audience_id: int, how: str) -> None:
        """The first hand-off wins: a later export or report doesn't move it."""
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO handoffs VALUES (?, ?, ?)", (audience_id, _now(), how))

    def exclude_members(self, audience_id: int, profile_ids: set[str], reason: str) -> None:
        """Leave lines out of both groups from now on (both, so the comparison stays like for like)."""
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE audience_members SET excluded = ? WHERE audience_id = ? AND profile_id = ? AND excluded IS NULL",
                ((reason, audience_id, pid) for pid in profile_ids))

    def excluded_count(self, audience_id: int) -> int:
        with self._lock:
            return self._db.execute("SELECT count(*) FROM audience_members WHERE audience_id = ? AND excluded IS NOT NULL",
                                    (audience_id,)).fetchone()[0]

    def report_outcome(self, row: dict[str, Any]) -> bool:
        """Store a reported outcome as pending. A new report replaces a pending one (a corrected file);
        False if the outcome was already confirmed, which nothing here can overwrite."""
        with self._lock, self._db:
            cur = self._db.execute(
                f"""INSERT INTO outcomes (audience_id, recorded_at, {', '.join(_OUTCOME_COLUMNS)})
                    VALUES (:audience_id, :recorded_at, {', '.join(':' + c for c in _OUTCOME_COLUMNS)})
                    ON CONFLICT (audience_id) DO UPDATE SET recorded_at = excluded.recorded_at,
                    {', '.join(f'{c} = excluded.{c}' for c in _OUTCOME_COLUMNS)}, reports = outcomes.reports + 1
                    WHERE outcomes.status = 'pending'""",
                {"recorded_at": _now(), **row},
            )
            self._db.execute(
                """INSERT INTO outcome_reports VALUES (:audience_id, :recorded_at, :source, :sent_at, :contacted,
                   :contacted_responders, :held_out, :held_out_responders, :unknown_responders, :reporter_json, :accepted)""",
                {"recorded_at": _now(), **row, "accepted": int(cur.rowcount == 1)})
            return cur.rowcount == 1

    def outcome_reports(self, audience_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM outcome_reports WHERE audience_id = ? ORDER BY recorded_at",
                                    (audience_id,)).fetchall()
        return [dict(r) for r in rows]

    def confirm_outcome(self, audience_id: int, confirmed_by: str, confirmer: dict[str, Any]) -> bool:
        """A person checked the reported numbers; only now does the outcome join the evidence."""
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE outcomes SET status = 'confirmed', confirmed_by = ?, confirmed_at = ?, confirmer_json = ? "
                "WHERE audience_id = ? AND status = 'pending'", (confirmed_by, _now(), json.dumps(confirmer), audience_id))
            return cur.rowcount == 1

    def record_advice(self, row: dict[str, Any]) -> int:
        """What the advisor was shown, what it proposed, and what the checks let through or withheld."""
        with self._lock, self._db:
            cur = self._db.execute(
                """INSERT INTO advice (created_at, request_id, campaign_type, goal_masked, facts_json, raw_json,
                   accepted_json, rejected_json, model, prompt_version) VALUES (:created_at, :request_id, :campaign_type,
                   :goal_masked, :facts_json, :raw_json, :accepted_json, :rejected_json, :model, :prompt_version)""",
                {"created_at": _now(), "request_id": None, **row})
            return cur.lastrowid

    def advised_conditions(self, advice_id: int | None, request_id: str | None) -> list[dict[str, Any]]:
        """Conditions that one recorded advice response, given for this request, actually proposed: the
        only ones that may keep the "advisor" label when a client sends them back."""
        if advice_id is None or request_id is None:
            return []
        with self._lock:
            row = self._db.execute("SELECT accepted_json FROM advice WHERE id = ? AND request_id = ?",
                                   (advice_id, request_id)).fetchone()
        return [] if row is None else [
            c for p in json.loads(row[0]) for c in (p.get("consequence", {}).get("definition") or {}).get("conditions", [])
            if c.get("source") == "advisor"]

    def outcomes(self, campaign_type: str | None = None, include_pending: bool = False,
                 audience_id: int | None = None) -> list[dict[str, Any]]:
        """Outcomes with the approval they belong to (name, goal, what was expected). Evidence uses confirmed
        outcomes only; the scorecard also shows the pending ones."""
        with self._lock:
            rows = self._db.execute(
                """SELECT o.*, a.name, a.measurement_json FROM outcomes o JOIN audiences a ON a.id = o.audience_id
                   WHERE (? IS NULL OR o.campaign_type = ?) AND (? OR o.status = 'confirmed')
                   AND (? IS NULL OR o.audience_id = ?) ORDER BY o.sent_at, o.audience_id""",
                (campaign_type, campaign_type, include_pending, audience_id, audience_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def evidence_outcomes(self, campaign_type: str | None = None) -> list[dict[str, Any]]:
        """Confirmed outcomes that may join the pooled evidence: not those of audiences approved while a
        large share of their lines was in another recent approval. Their holdouts and sends overlap, so
        they aren't independent campaigns; the scorecard still shows them, marked."""
        return [o for o in self.outcomes(campaign_type)
                if not json.loads(o["measurement_json"]).get("overlap_note")]

    def counts(self) -> dict[str, int]:
        """How far decisions have travelled round the loop."""
        with self._lock:
            interpreted = self._db.execute("SELECT count(*) FROM interpretations WHERE parent_request_id IS NULL").fetchone()[0]
            approved = self._db.execute("SELECT count(*) FROM audiences").fetchone()[0]
            typed = self._db.execute(
                "SELECT count(*) FROM audiences WHERE json_extract(measurement_json, '$.campaign_type') IS NOT NULL").fetchone()[0]
            measured = self._db.execute("SELECT count(*) FROM outcomes WHERE status = 'confirmed'").fetchone()[0]
            pending = self._db.execute("SELECT count(*) FROM outcomes WHERE status = 'pending'").fetchone()[0]
        return {"interpreted": interpreted, "approved": approved, "approved_with_type": typed, "measured": measured,
                "pending_confirmation": pending}

    def get_audience(self, audience_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM audiences WHERE id = ?", (audience_id,)).fetchone()
        return dict(row) if row else None

    def list_audiences(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT a.*, i.request_text, i.model, i.prompt_version, i.definition_json AS proposed_definition_json
                   FROM audiences a JOIN interpretations i USING (request_id)
                   ORDER BY a.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def interpretation_signals(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Recent interpretation outcomes, for the insights view. A refinement carries its parent's open
        terms and unmapped phrases forward, so the parent's lists come along to count only what is new."""
        with self._lock:
            rows = self._db.execute(
                """SELECT i.created_at, i.request_text, i.status, i.unmapped_json, i.clarifications_json, i.issues_json,
                          i.parent_request_id, p.unmapped_json AS parent_unmapped_json,
                          p.clarifications_json AS parent_clarifications_json
                   FROM interpretations i LEFT JOIN interpretations p ON p.request_id = i.parent_request_id
                   ORDER BY i.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


class TTLCache:
    """Small LRU cache with expiry. Holds raw model proposals, keyed by request + versions."""

    def __init__(self, ttl_s: int, max_entries: int):
        self._ttl = ttl_s
        self._max = max_entries
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stored_at, value = item
            if time.monotonic() - stored_at > self._ttl:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return value

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._items[key] = (time.monotonic(), value)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
