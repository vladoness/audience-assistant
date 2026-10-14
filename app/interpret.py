"""Turns a marketer's request into a validated, explained audience proposal.

Pipeline (one request):
  mask PII -> cache lookup -> model proposal -> expand glossary terms -> validate
  -> (one repair attempt if invalid) -> derive status -> explain -> record + metrics

A refinement ("now drop the Coast") runs the same pipeline on a delta applied to the audience on
screen (see app/refine.py), without the cache, and records a link to the interpretation it refines.

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
from app.limits import DailyBudget
from app.governance import reconcile_with_proposal, verify_provenance
from app.llm import (
    MAX_OUTPUT_TOKENS, PROMPT_VERSION, REFINE_SCHEMA, REFINE_VERSION, BudgetExhausted, CircuitOpen, NotRecorded, LLMResult,
    Pricing, Provider, ProviderUnavailable, build_messages, build_refine_messages, build_system_prompt,
)
from app.models import (
    AudienceDefinition, CheckResult, Clarification, ClarificationOption, Condition, InterpretResult, Issue, Telemetry,
)
from app.observability import Metrics
from app.pii import mask_pii
from app.refine import answered_terms, describe_changes, model_state, plan_refinement
from app.store import AuditStore, TTLCache
from app.validate import parse_condition, validate_definition

log = logging.getLogger(__name__)

_NOT_REPAIRABLE = {"policy_violation"}
_REFINE_PROMPT_VERSION = f"{PROMPT_VERSION}+{REFINE_VERSION}"


class RefineRejected(ValueError):
    """The change text can't be used (empty or too long). Nothing is recorded or changed."""


class Interpreter:
    def __init__(self, catalog: Catalog, provider: Provider, store: AuditStore, cache: TTLCache,
                 metrics: Metrics, pricing: Pricing, max_request_chars: int = 500, budget: DailyBudget | None = None,
                 breaker_threshold: int = 3, breaker_cooldown_s: float = 30.0):
        self.catalog = catalog
        self.provider = provider
        self.store = store
        self.cache = cache
        self.metrics = metrics
        self.pricing = pricing
        self.max_request_chars = max_request_chars
        self.budget = budget
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown_s = breaker_cooldown_s
        self._consecutive_failures = 0
        self._open_until = 0.0
        self.system_prompt = build_system_prompt(catalog)

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._open_until

    def interpret(self, request_text: str, request_id: str | None = None, origin: str = "ui") -> InterpretResult:
        started = time.perf_counter()
        request_id = request_id if request_id and request_id != "-" else uuid.uuid4().hex[:12]
        text = request_text.strip()
        tel = Telemetry(provider=self.provider.name, origin=origin, model=self.provider.model,
                        prompt_version=PROMPT_VERSION)

        if len(text) > self.max_request_chars:
            return self._finish(request_id, "[request too long, not stored]", None, self._empty("invalid", request_id, tel, Issue(
                level="error", code="request_too_long",
                message=f"Requests are limited to {self.max_request_chars} characters; describe one audience at a time.")), started)
        masked, pii_count = mask_pii(text)
        tel.pii_masked = pii_count
        if not masked:
            return self._finish(request_id, masked, None, self._empty("invalid", request_id, tel,
                                Issue(level="error", code="empty_request", message="Describe the audience you need.")), started)

        cache_key = TTLCache.key(" ".join(masked.lower().split()), self.provider.model, PROMPT_VERSION,
                                 self.catalog.fingerprint)
        proposal = self.cache.get(cache_key)
        if proposal is not None:
            tel.cache_hit = True
            self.metrics.incr("cache_hit")
        else:
            try:
                proposal = self._call_model(build_messages(masked), tel)
            except ProviderUnavailable as exc:
                log.warning("llm_unavailable", extra={"reason": str(exc), "code": exc.code})
                self.metrics.incr(exc.code)
                result = self._empty("ai_unavailable", request_id, tel, Issue(
                    level="warning", code=exc.code,
                    message=f"The assistant is unavailable ({exc}). Build the audience with the editor below."))
                return self._finish(request_id, masked, None, result, started)

        result = self._evaluate(request_id, proposal, tel, masked)
        errors = [i for i in result.issues if i.level == "error"]
        if errors and not tel.cache_hit and not all(e.code in _NOT_REPAIRABLE for e in errors):
            try:
                repaired = self._call_model(
                    build_messages(masked, repair={"proposal": proposal, "errors": [e.message for e in errors]}), tel)
                tel.repaired = True
                self.metrics.incr("repair_attempt")
                proposal = repaired
                result = self._evaluate(request_id, proposal, tel, masked)
            except ProviderUnavailable as exc:
                log.warning("llm_repair_unavailable", extra={"reason": str(exc)})

        if result.status in {"ready", "needs_clarification", "needs_confirmation"} and not tel.cache_hit:
            self.cache.put(cache_key, proposal)
        return self._finish(request_id, masked, proposal, result, started)

    def refine(self, parent: dict[str, Any], current: AudienceDefinition, change_text: str,
               request_id: str | None = None, origin: str = "ui") -> InterpretResult:
        """Apply a plain-language change to the audience on screen.

        `parent` is the audit row of the interpretation being refined. The definition on screen is
        re-verified (governed labels, model provenance) before the model sees it. If the model is
        unavailable, ProviderUnavailable propagates and nothing is recorded: the screen and the
        parent interpretation stay valid.
        """
        started = time.perf_counter()
        request_id = request_id if request_id and request_id != "-" else uuid.uuid4().hex[:12]
        text = change_text.strip()
        if len(text) > self.max_request_chars:
            raise RefineRejected(f"Changes are limited to {self.max_request_chars} characters.")
        masked, pii_count = mask_pii(text)
        if not masked:
            raise RefineRejected("Describe the change you want.")
        tel = Telemetry(provider=self.provider.name, origin=origin, model=self.provider.model,
                        prompt_version=_REFINE_PROMPT_VERSION, pii_masked=pii_count)

        parent_proposed = AudienceDefinition(**json.loads(parent["definition_json"]))
        current, _ = validate_definition(current, self.catalog)
        current, _ = reconcile_with_proposal(verify_provenance(current, self.catalog), parent_proposed)
        answered = answered_terms(current)
        pending = [t for t in json.loads(parent["clarifications_json"]) if t not in answered]
        carried_unmapped = json.loads(parent["unmapped_json"])
        state = model_state(current, pending)

        try:
            delta = self._call_model(build_refine_messages(state, masked), tel, schema=REFINE_SCHEMA)
        except ProviderUnavailable as exc:
            log.warning("refine_unavailable", extra={"reason": str(exc), "code": exc.code,
                                                     "parent_request_id": parent["request_id"]})
            self.metrics.incr(f"refine_{exc.code}")
            raise

        result = self._evaluate_refinement(request_id, current, delta, pending, carried_unmapped, tel, masked)
        new_attrs = {str(c.get("attribute")) for c in delta.get("add_conditions") or [] if isinstance(c, dict)}
        repairable = [i for i in result.issues if i.level == "error" and i.code not in _NOT_REPAIRABLE
                      and (i.attribute in new_attrs or i.code in {"unknown_term", "malformed_condition", "unknown_operator"})]
        if repairable:
            try:
                delta = self._call_model(build_refine_messages(
                    state, masked, repair={"proposal": delta, "errors": [i.message for i in repairable]}),
                    tel, schema=REFINE_SCHEMA)
                tel.repaired = True
                self.metrics.incr("repair_attempt")
                result = self._evaluate_refinement(request_id, current, delta, pending, carried_unmapped, tel, masked)
            except ProviderUnavailable as exc:
                log.warning("llm_repair_unavailable", extra={"reason": str(exc)})

        result.parent_request_id = parent["request_id"]
        result.changes = describe_changes(current, result.definition, self.catalog)
        self.metrics.incr("refinements")
        return self._finish(request_id, masked, delta, result, started, parent_request_id=parent["request_id"],
                            prompt_version=_REFINE_PROMPT_VERSION)

    def review(self, row: dict[str, Any]) -> InterpretResult:
        """A recorded interpretation, re-checked against today's governance, for a person to review and
        approve (for example one an agent drafted through the MCP server). No model call."""
        definition = AudienceDefinition(**json.loads(row["definition_json"]))
        unmapped = json.loads(row["unmapped_json"])
        checked = self.check(definition, json.loads(row["clarifications_json"]), {})
        status = "needs_confirmation" if checked.status == "ready" and unmapped else checked.status
        return InterpretResult(
            request_id=row["request_id"], status=status, definition=checked.definition,
            clarifications=checked.clarifications, unmapped_phrases=unmapped, issues=checked.issues,
            explanation=checked.explanation, telemetry=Telemetry(**json.loads(row["telemetry_json"])),
            parent_request_id=row["parent_request_id"], request_text=row["request_text"],
        )

    def _evaluate_refinement(self, request_id: str, current: AudienceDefinition, delta: dict[str, Any],
                             pending: list[str], carried_unmapped: list[str], tel: Telemetry,
                             change_text: str) -> InterpretResult:
        plan = plan_refinement(current, delta, pending, carried_unmapped)
        result = self._evaluate(request_id, plan.proposal, tel, change_text, kept=plan.kept,
                                carried_pending=plan.pending_terms)
        result.issues = plan.issues + result.issues
        return result

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
        validated = verify_provenance(validated, self.catalog)
        issues += problems
        clarifications = [self._clarification(t) for t in still_pending]
        status = "invalid" if any(i.level == "error" for i in issues) else ("needs_clarification" if clarifications else "ready")
        self.metrics.incr(f"check_{status}")
        return CheckResult(status=status, definition=validated, clarifications=clarifications, issues=issues,
                           explanation=explain(validated, self.catalog))

    def _call_model(self, messages: list[dict[str, Any]], tel: Telemetry,
                    schema: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.budget is not None and not self.budget.available():
            raise BudgetExhausted(f"today's model budget of ${self.budget.limit_usd:.2f} is used up")
        if self.circuit_open:
            raise CircuitOpen("several recent model calls failed; pausing the assistant briefly")
        try:
            result: LLMResult = self.provider.propose(self.system_prompt, messages, schema)
        except NotRecorded:
            raise
        except ProviderUnavailable:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.breaker_threshold:
                self._open_until = time.monotonic() + self.breaker_cooldown_s
                log.warning("llm_circuit_opened", extra={"cooldown_s": self.breaker_cooldown_s})
            raise
        self._consecutive_failures = 0
        cost = self.pricing.cost(result)
        if self.budget is not None:
            self.budget.spend(cost)
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
        self.metrics.incr("llm_input_tokens", result.total_input_tokens)
        self.metrics.incr("llm_output_tokens", result.output_tokens)
        if not result.replayed:
            self.metrics.observe("llm_ms", result.latency_ms)
        log.info("llm_call", extra={
            "gen_ai.provider.name": self.provider.name,
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": self.provider.model,
            "gen_ai.response.model": result.model,
            "gen_ai.request.max_tokens": MAX_OUTPUT_TOKENS,
            "llm.effort": getattr(self.provider, "effort", None),
            "gen_ai.response.finish_reasons": [result.finish_reason],
            "gen_ai.usage.input_tokens": result.total_input_tokens,
            "gen_ai.usage.cache_read.input_tokens": result.cache_read_tokens,
            "gen_ai.usage.cache_creation.input_tokens": result.cache_write_tokens,
            "gen_ai.usage.output_tokens": result.output_tokens,
            "latency_ms": round(result.latency_ms, 1),
            "cost_usd": cost,
            "replayed": result.replayed,
        })
        return result.proposal

    def _evaluate(self, request_id: str, proposal: dict[str, Any], tel: Telemetry, request_text: str,
                  kept: list[Condition] | None = None, carried_pending: list[str] | None = None) -> InterpretResult:
        """`kept` and `carried_pending` are for refinements: conditions already on screen (with their
        sources) and open terms still waiting for an answer."""
        issues: list[Issue] = []
        conditions: list[Condition] = list(kept or [])
        clarifications: list[Clarification] = [
            self._clarification(self.catalog.terms[t]) for t in carried_pending or [] if t in self.catalog.terms
        ]

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
                continue
            if not _mentions(request_text, term):
                issues.append(Issue(
                    level="warning", code="ungrounded_term",
                    message=f"The assistant applied the governed term '{term.name.replace('_', ' ')}', but your request "
                            "doesn't use any of its phrases. Remove those conditions if that isn't what you meant."))
            if term.status == "decided":
                conditions += [Condition(**c, source=f"glossary:{term.name}") for c in term.conditions]
            else:
                clarifications.append(self._clarification(term))

        channel = proposal.get("channel")
        if channel in (None, "", "unspecified"):
            channel = self.catalog.default_channel
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
        elif unmapped and not definition.conditions:
            status = "unsupported"
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
                result: InterpretResult, started: float, parent_request_id: str | None = None,
                prompt_version: str = PROMPT_VERSION) -> InterpretResult:
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
            "clarifications_json": json.dumps([c.term for c in result.clarifications]),
            "issues_json": json.dumps([{"code": i.code, "attribute": i.attribute, "message": i.message}
                                       for i in result.issues]),
            "model": result.telemetry.model,
            "prompt_version": prompt_version,
            "governance_versions": json.dumps(self.catalog.versions),
            "telemetry_json": result.telemetry.model_dump_json(),
            "parent_request_id": parent_request_id,
        })
        log.info("interpretation", extra={
            "origin": result.telemetry.origin, "parent_request_id": parent_request_id,
            "status": result.status, "conditions": len(result.definition.conditions),
            "clarifications": [c.term for c in result.clarifications], "unmapped": len(result.unmapped_phrases),
            "issue_codes": [i.code for i in result.issues], "cache_hit": result.telemetry.cache_hit,
            "repaired": result.telemetry.repaired, "llm_calls": result.telemetry.llm_calls,
            "total_ms": result.telemetry.total_ms, "cost_usd": result.telemetry.cost_usd,
            "pii_masked": result.telemetry.pii_masked,
        })
        return result


def _mentions(request_text: str, term) -> bool:
    """Deterministic grounding check: does the request contain one of the term's phrases?"""
    text = request_text.lower()
    return any(alias.lower() in text for alias in term.aliases) or term.name.replace("_", " ") in text
