"""Persistence: the audit trail (SQLite) and the interpretation cache (in memory).

Every interpretation is recorded with the masked request, the raw model proposal, the model
and prompt version, and its telemetry. An approval references that record, so the audit trail
shows what the marketer asked, what the model proposed, what the marketer changed and what
was finally approved, with the catalog and glossary versions in force.
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interpretations (
    request_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    request_text TEXT NOT NULL,
    status TEXT NOT NULL,
    proposal_json TEXT,
    definition_json TEXT NOT NULL,
    unmapped_json TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT,
    catalog_version TEXT NOT NULL,
    glossary_version TEXT NOT NULL,
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
    catalog_version TEXT NOT NULL,
    glossary_version TEXT NOT NULL
);
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
        self._db.executescript(_SCHEMA)

    def record_interpretation(self, row: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO interpretations VALUES (:request_id, :created_at, :request_text, :status, :proposal_json,
                   :definition_json, :unmapped_json, :model, :prompt_version, :catalog_version, :glossary_version,
                   :telemetry_json)""",
                {"created_at": _now(), **row},
            )

    def get_interpretation(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM interpretations WHERE request_id = ?", (request_id,)).fetchone()
        return dict(row) if row else None

    def record_approval(self, row: dict[str, Any]) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                """INSERT INTO audiences (created_at, name, approver, request_id, definition_json, definition_hash,
                   edited_by_user, explanation, channel, matched, reachable, acknowledged_unmapped_json,
                   catalog_version, glossary_version)
                   VALUES (:created_at, :name, :approver, :request_id, :definition_json, :definition_hash,
                   :edited_by_user, :explanation, :channel, :matched, :reachable, :acknowledged_unmapped_json,
                   :catalog_version, :glossary_version)""",
                {"created_at": _now(), **row},
            )
            return cur.lastrowid

    def list_audiences(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT a.*, i.request_text, i.model, i.prompt_version
                   FROM audiences a JOIN interpretations i USING (request_id)
                   ORDER BY a.id DESC LIMIT ?""",
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
