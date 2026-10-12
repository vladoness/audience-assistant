"""Persistence: the audit trail (SQLite) and the interpretation cache (in memory).

Every interpretation is recorded with the masked request, the raw model proposal, the model
and prompt version, and the versions of every governance file. An approval references that
record and adds a governance snapshot: which governed terms it used, under which decision and
parameter versions. That is what lets the system tell, later, which approved audiences a
glossary or parameter change affects.
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

SCHEMA_VERSION = 3

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
    telemetry_json TEXT NOT NULL
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
    governance_versions TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS audiences_one_per_request ON audiences(request_id);
"""


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
            for table in ("interpretations", "audiences"):
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
                   :governance_versions, :telemetry_json)""",
                {"created_at": _now(), **row},
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

    def record_approval(self, row: dict[str, Any]) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                """INSERT INTO audiences (created_at, name, approver, request_id, definition_json, definition_hash,
                   edited_by_user, explanation, channel, matched, reachable, acknowledged_unmapped_json,
                   governance_json, governance_versions)
                   VALUES (:created_at, :name, :approver, :request_id, :definition_json, :definition_hash,
                   :edited_by_user, :explanation, :channel, :matched, :reachable, :acknowledged_unmapped_json,
                   :governance_json, :governance_versions)""",
                {"created_at": _now(), **row},
            )
            return cur.lastrowid

    def record_approval_once(self, row: dict[str, Any]) -> int | None:
        """Insert an approval; None if this interpretation was approved concurrently."""
        try:
            return self.record_approval(row)
        except sqlite3.IntegrityError:
            return None

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
        """Recent interpretation outcomes, for the insights view."""
        with self._lock:
            rows = self._db.execute(
                """SELECT created_at, request_text, status, unmapped_json, clarifications_json, issues_json
                   FROM interpretations ORDER BY created_at DESC LIMIT ?""",
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
