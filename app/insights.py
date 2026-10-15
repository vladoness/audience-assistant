"""Field-to-product signals, computed from the audit trail.

What marketers asked for that the data can't express is counted, not discarded. That is the
backlog for the data team (which fields to add) and for governance (which open terms to settle
first, and which way people already lean). Edits marketers make to the AI's proposals show
where the prompt or the catalog descriptions need work; refinements show how often the first
answer wasn't the audience they meant.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from app.catalog import Catalog, parse_pending
from app.store import AuditStore


def build_insights(store: AuditStore, catalog: Catalog) -> dict[str, Any]:
    signals = store.interpretation_signals()
    audiences = store.list_audiences(limit=1000)

    demand: Counter[str] = Counter()
    example: dict[str, str] = {}
    last_seen: dict[str, str] = {}
    asked: Counter[str] = Counter()
    blocked: Counter[tuple[str, str]] = Counter()
    for s in signals:
        for issue in json.loads(s["issues_json"]):
            if issue["code"] == "policy_violation":
                policy = re.search(r"policy (\S+)", issue["message"])
                blocked[(issue["attribute"], policy.group(1) if policy else "?")] += 1
        carried_unmapped = set(json.loads(s["parent_unmapped_json"] or "[]"))
        carried_terms = {parse_pending(t)[0] for t in json.loads(s["parent_clarifications_json"] or "[]")}
        for phrase in json.loads(s["unmapped_json"]):
            if phrase in carried_unmapped:
                continue
            key = _normalize(phrase)
            demand[key] += 1
            example.setdefault(key, s["request_text"])
            last_seen.setdefault(key, s["created_at"])
        asked.update(t for t in (parse_pending(n)[0] for n in json.loads(s["clarifications_json"])) if t not in carried_terms)

    chosen: dict[str, Counter[str]] = defaultdict(Counter)
    edited_fields: Counter[str] = Counter()
    for a in audiences:
        approved = json.loads(a["definition_json"])["conditions"]
        choices = {c["source"].removeprefix("glossary:") for c in approved
                   if c["source"].startswith("glossary:") and "=" in c["source"]}
        for choice in choices:
            term, _, option = choice.partition("=")
            chosen[term][option] += 1
        if a["edited_by_user"]:
            proposed = {_key(c) for c in json.loads(a["proposed_definition_json"])["conditions"] if c["source"] == "model"}
            kept = {_key(c) for c in approved if c["source"] == "model"}
            changed = {c["attribute"] for c in approved if c["source"] == "user"} | {k[0] for k in proposed - kept}
            edited_fields.update(changed)

    edited = sum(1 for a in audiences if a["edited_by_user"])
    return {
        "requests": sum(1 for s in signals if not s["parent_request_id"]),
        "refinements": sum(1 for s in signals if s["parent_request_id"]),
        "outcomes": dict(Counter(s["status"] for s in signals)),
        "unmapped_demand": [
            {"phrase": phrase, "count": n, "last_seen": last_seen[phrase], "example": example[phrase]}
            for phrase, n in demand.most_common(10)
        ],
        "blocked_by_policy": [
            {"field": f, "label": catalog.attributes[f].label if f in catalog.attributes else f, "policy": p, "count": n}
            for (f, p), n in blocked.most_common(8)
        ],
        "open_terms": [
            {"term": t.name, "asked": asked[t.name], "owner_pending": t.owner_pending,
             "chosen": {o.label: chosen[t.name][o.id] for o in t.options}}
            for t in catalog.terms.values() if t.status == "open"
        ],
        "edits": {
            "approved": len(audiences),
            "edited": edited,
            "rate": round(edited / len(audiences), 3) if audiences else 0.0,
            "by_field": [
                {"field": f, "label": catalog.attributes[f].label if f in catalog.attributes else f, "count": n}
                for f, n in edited_fields.most_common(8)
            ],
        },
    }


def _normalize(phrase: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", phrase.lower())).strip()


def _key(c: dict[str, Any]) -> tuple[str, str, str]:
    return c["attribute"], c["op"], json.dumps(c["value"])
