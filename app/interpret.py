"""Turns a marketer's request into a validated, explained audience proposal.

Pipeline (one request):
  mask PII -> cache lookup -> model proposal -> expand glossary terms -> validate
  -> (one repair attempt if invalid) -> derive status -> explain -> record + metrics

The status is derived here from the validated result, never taken from the model. The model
can say "done"; only the validator can make a proposal "ready".
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from app.catalog import Catalog
from app.explain import explain
from app.llm import PROMPT_VERSION, LLMResult, Pricing, Provider, ProviderUnavailable, build_messages, build_system_prompt
from app.models import (
    AudienceDefinition, CheckResult, Clarification, ClarificationOption, Condition, InterpretResult, Issue, Telemetry,
)
from app.observability import Metrics
from app.pii import mask_pii
from app.store import AuditStore, TTLCache
from app.validate import parse_condition, validate_definition

log = logging.getLogger(__name__)

_NOT_REPAIRABLE = {"restricted_attribute", "consent_not_targetable"}


class Interpreter:
    def __init__(self, catalog: Catalog, provider: Provider, store: AuditStore, cache: TTLCache,
                 metrics: Metrics, pricing: Pricing, max_request_chars: int = 500):
        self.catalog = catalog
        self.provider = provider
        self.store = store
        self.cache = cache
        self.metrics = metrics
        self.pricing = pricing
        self.max_request_chars = max_request_chars
        self.system_prompt = build_system_prompt(catalog)

    def interpret(self, request_text: str) -> InterpretResult:
        started = time.perf_counter()
        request_id = uuid.uuid4().hex[:12]
        masked, pii_count = mask_pii(request_text.strip())
        tel = Telemetry(provider=self.provider.name, model=self.provider.model, prompt_version=PROMPT_VERSION,
                        pii_masked=pii_count)

        if not masked:
            return self._finish(request_id, masked, None, self._empty("invalid", request_id, tel,
                                Issue(level="error", code="empty_request", message="Describe the audience you need.")), started)
        if len(masked) > self.max_request_chars:
            return self._finish(request_id, masked, None, self._empty("invalid", request_id, tel, Issue(
                level="error", code="request_too_long",
                message=f"Requests are limited to {self.max_request_chars} characters; describe one audience at a time.")), started)

        cache_key = TTLCache.key(" ".join(masked.lower().split()), self.provider.model, PROMPT_VERSION,
                                 self.catalog.version, self.catalog.glossary_version)
        proposal = self.cache.get(cache_key)
        if proposal is not None:
            tel.cache_hit = True
            self.metrics.incr("cache_hit")
        else:
            try:
                proposal = self._call_model(build_messages(masked), tel)
            except ProviderUnavailable as exc:
                log.warning("llm_unavailable", extra={"reason": str(exc)})
                self.metrics.incr("llm_unavailable")
                result = self._empty("ai_unavailable", request_id, tel, Issue(
                    level="warning", code="ai_unavailable",
                    message=f"The assistant is unavailable ({exc}). Build the audience with the editor below."))
                return self._finish(request_id, masked, None, result, started)

        result = self._evaluate(request_id, proposal, tel)
        errors = [i for i in result.issues if i.level == "error"]
        if errors and not tel.cache_hit and not all(e.code in _NOT_REPAIRABLE for e in errors):
            try:
                repaired = self._call_model(
                    build_messages(masked, repair={"proposal": proposal, "errors": [e.message for e in errors]}), tel)
                tel.repaired = True
                self.metrics.incr("repair_attempt")
                proposal = repaired
                result = self._evaluate(request_id, proposal, tel)
            except ProviderUnavailable as exc:
                log.warning("llm_repair_unavailable", extra={"reason": str(exc)})

        if result.status in {"ready", "needs_clarification", "needs_confirmation"} and not tel.cache_hit:
            self.cache.put(cache_key, proposal)
        return self._finish(request_id, masked, proposal, result, started)

    def check(self, definition: AudienceDefinition, pending_terms: list[str], resolutions: dict[str, str]) -> CheckResult:
        """Deterministic re-evaluation after the marketer edits conditions or answers a clarification."""
        conditions = list(definition.conditions)
        issues: list[Issue] = []
        still_pending = []
        for term_name in pending_terms:
            term = self.catalog.terms.get(term_name)
            option_id = resolutions.get(term_name)
            option = term.option(option_id) if term and option_id else None
            if term is None:
                issues.append(Issue(level="error", code="unknown_term", message=f"Unknown glossary term '{term_name}'."))
            elif option is None:
                still_pending.append(term)
            else:
                conditions += [Condition(**c, source=f"glossary:{term.name}={option.id}") for c in option.conditions]

        validated, problems = validate_definition(definition.model_copy(update={"conditions": conditions}), self.catalog)
        issues += problems
        clarifications = [self._clarification(t) for t in still_pending]
        status = "invalid" if any(i.level == "error" for i in issues) else ("needs_clarification" if clarifications else "ready")
        self.metrics.incr(f"check_{status}")
        return CheckResult(status=status, definition=validated, clarifications=clarifications, issues=issues,
                           explanation=explain(validated, self.catalog))

    def _call_model(self, messages: list[dict[str, Any]], tel: Telemetry) -> dict[str, Any]:
        result: LLMResult = self.provider.propose(self.system_prompt, messages)
        cost = self.pricing.cost(result)
        tel.llm_calls += 1
        tel.model = result.model
        tel.llm_ms += round(result.latency_ms, 1)
        tel.input_tokens += result.input_tokens
        tel.output_tokens += result.output_tokens
        tel.cache_read_tokens += result.cache_read_tokens
        tel.cache_write_tokens += result.cache_write_tokens
        tel.cost_usd = round(tel.cost_usd + cost, 6)
        self.metrics.incr("llm_calls")
        self.metrics.incr("llm_cost_usd", cost)
        self.metrics.incr("llm_input_tokens", result.input_tokens + result.cache_read_tokens + result.cache_write_tokens)
        self.metrics.incr("llm_output_tokens", result.output_tokens)
        self.metrics.observe("llm_ms", result.latency_ms)
        log.info("llm_call", extra={"model": result.model, "latency_ms": round(result.latency_ms, 1),
                                    "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                                    "cache_read_tokens": result.cache_read_tokens, "cost_usd": cost,
                                    "replayed": result.replayed})
        return result.proposal

    def _evaluate(self, request_id: str, proposal: dict[str, Any], tel: Telemetry) -> InterpretResult:
        issues: list[Issue] = []
        conditions: list[Condition] = []
        clarifications: list[Clarification] = []

        raw_conditions = proposal.get("conditions") or []
        if not isinstance(raw_conditions, list):
            raw_conditions = []
            issues.append(Issue(level="error", code="malformed_proposal", message="Proposal conditions were not a list."))
        for raw in raw_conditions:
            cond, problem = parse_condition(raw, source="model")
            if problem:
                issues.append(problem)
            else:
                conditions.append(cond)

        for term_name in dict.fromkeys(proposal.get("glossary_terms") or []):
            term = self.catalog.terms.get(term_name)
            if term is None:
                issues.append(Issue(level="error", code="unknown_term", message=f"'{term_name}' is not a glossary term."))
            elif term.status == "decided":
                conditions += [Condition(**c, source=f"glossary:{term.name}") for c in term.conditions]
            else:
                clarifications.append(self._clarification(term))

        channel = proposal.get("channel") or self.catalog.default_channel
        if channel not in self.catalog.channels:
            issues.append(Issue(level="warning", code="unknown_channel",
                                message=f"Unknown channel '{channel}', using {self.catalog.default_channel}."))
            channel = self.catalog.default_channel

        definition, problems = validate_definition(AudienceDefinition(conditions=conditions, channel=channel), self.catalog)
        issues += problems
        unmapped = [str(p) for p in proposal.get("unmapped_phrases") or []]

        if proposal.get("out_of_scope") and not definition.conditions and not clarifications:
            status = "unsupported"
        elif any(i.level == "error" for i in issues):
            status = "invalid"
        elif clarifications:
            status = "needs_clarification"
        elif unmapped:
            status = "needs_confirmation"
        elif not definition.conditions:
            status = "unsupported" if not raw_conditions else "invalid"
        else:
            status = "ready"

        return InterpretResult(
            request_id=request_id, status=status, definition=definition, clarifications=clarifications,
            unmapped_phrases=unmapped, assumptions=[str(a) for a in proposal.get("assumptions") or []][:3],
            issues=issues, explanation=explain(definition, self.catalog), telemetry=tel,
        )

    def _clarification(self, term) -> Clarification:
        return Clarification(term=term.name, question=term.question,
                             options=[ClarificationOption(id=o.id, label=o.label) for o in term.options])

    def _empty(self, status: str, request_id: str, tel: Telemetry, issue: Issue) -> InterpretResult:
        definition = AudienceDefinition(channel=self.catalog.default_channel)
        return InterpretResult(request_id=request_id, status=status, definition=definition, issues=[issue],
                               explanation=explain(definition, self.catalog), telemetry=tel)

    def _finish(self, request_id: str, masked: str, proposal: dict[str, Any] | None,
                result: InterpretResult, started: float) -> InterpretResult:
        result.telemetry.total_ms = round((time.perf_counter() - started) * 1000, 1)
        self.metrics.incr(f"interpret_{result.status}")
        self.metrics.observe("interpret_ms", result.telemetry.total_ms)
        self.store.record_interpretation({
            "request_id": request_id,
            "request_text": masked,
            "status": result.status,
            "proposal_json": json.dumps(proposal) if proposal is not None else None,
            "definition_json": result.definition.model_dump_json(),
            "unmapped_json": json.dumps(result.unmapped_phrases),
            "model": result.telemetry.model,
            "prompt_version": PROMPT_VERSION,
            "catalog_version": self.catalog.version,
            "glossary_version": self.catalog.glossary_version,
            "telemetry_json": result.telemetry.model_dump_json(),
        })
        log.info("interpretation", extra={
            "status": result.status, "conditions": len(result.definition.conditions),
            "clarifications": [c.term for c in result.clarifications], "unmapped": len(result.unmapped_phrases),
            "issue_codes": [i.code for i in result.issues], "cache_hit": result.telemetry.cache_hit,
            "repaired": result.telemetry.repaired, "llm_calls": result.telemetry.llm_calls,
            "total_ms": result.telemetry.total_ms, "cost_usd": result.telemetry.cost_usd,
            "pii_masked": result.telemetry.pii_masked,
        })
        return result
