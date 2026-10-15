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
import re
import time
import uuid
from typing import Any

from app.audience import AudienceEngine
from app.catalog import NOT, Catalog, invert, parse_pending, pending_name
from app.explain import explain
from app.limits import DailyBudget
from app.governance import canonical, reconcile_with_proposal, verify_provenance
from app.llm import (
    ADVICE_SCHEMA, ADVISOR_SYSTEM, ADVISOR_VERSION, PROMPT_VERSION, REFINE_SCHEMA, REFINE_VERSION,
    BudgetExhausted, CircuitOpen, NotRecorded, LLMResult, Pricing, Provider, ProviderUnavailable, build_advice_messages,
    build_messages, build_refine_messages, build_system_prompt, max_tokens,
)
from app.models import (
    AudienceDefinition, CheckResult, Clarification, ClarificationOption, Condition, InterpretResult, Issue, Telemetry,
)
from app.observability import Metrics
from app.pii import mask_pii
from app.refine import answered_terms, describe_changes, model_state, plan_refinement
from app.store import AuditStore, TTLCache
from app.validate import normalize, parse_condition, validate_definition

log = logging.getLogger(__name__)

_NOT_REPAIRABLE = {"policy_violation"}
_REFINE_PROMPT_VERSION = f"{PROMPT_VERSION}+{REFINE_VERSION}"


class RefineRejected(ValueError):
    """The change text can't be used (empty or too long). Nothing is recorded or changed."""


class Interpreter:
    def __init__(self, catalog: Catalog, provider: Provider, store: AuditStore, cache: TTLCache,
                 metrics: Metrics, pricing: Pricing, max_request_chars: int = 500, budget: DailyBudget | None = None,
                 breaker_threshold: int = 3, breaker_cooldown_s: float = 30.0, engine: AudienceEngine | None = None):
        self.catalog = catalog
        self.engine = engine  # when set, every clarification option shows what choosing it would reach
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
        pending = [t for t in json.loads(parent["clarifications_json"]) if parse_pending(t)[0] not in answered]
        carried_unmapped = json.loads(parent["unmapped_json"])
        # The model sees only conditions that validate: typed catalog values, so client text that failed
        # validation (or was never masked) can't reach the prompt. Invalid ones stay on screen, flagged.
        valid = [c for c in current.conditions if normalize(c, self.catalog, current.channel)[0] is not None]
        state = model_state(current.model_copy(update={"conditions": valid}), pending)

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

    def check(self, definition: AudienceDefinition, pending_terms: list[str], resolutions: dict[str, str],
              advice_id: int | None = None) -> CheckResult:
        """Deterministic re-evaluation after the marketer edits conditions or answers a clarification."""
        conditions = list(definition.conditions)
        issues: list[Issue] = []
        still_pending = []
        for name in pending_terms:
            term_name, negated = parse_pending(name)
            term = self.catalog.terms.get(term_name)
            option_id = resolutions.get(term_name)
            option = term.option(option_id) if term and option_id else None
            if option is not None and option.id.startswith(NOT) != negated:
                option = None  # "not active" is only answered by an exclusion, "active" only by a definition
            if term is None:
                issues.append(Issue(level="error", code="unknown_term", message=f"Unknown glossary term '{term_name}'."))
            elif option is None:
                still_pending.append((term, negated))
            else:
                conditions += [Condition(**c, source=f"glossary:{term.name}={option.id}") for c in option.conditions]

        validated, problems = validate_definition(definition.model_copy(update={"conditions": conditions}), self.catalog)
        validated = verify_provenance(validated, self.catalog, self.store.advised_conditions(advice_id))
        issues += problems
        clarifications: list[Clarification] = []
        for term, negated in still_pending:
            self._ask(term, negated, clarifications, issues)
        self._quantify(validated, clarifications, issues)
        status = "invalid" if any(i.level == "error" for i in issues) else ("needs_clarification" if clarifications else "ready")
        self.metrics.incr(f"check_{status}")
        return CheckResult(status=status, definition=validated, clarifications=clarifications, issues=issues,
                           explanation=explain(validated, self.catalog))

    def advise(self, facts: list[dict[str, Any]], goal: str, campaign_type: str | None) -> tuple[dict[str, Any], Telemetry]:
        """The advisor's raw proposals for these facts. Checking them is the caller's job (app/advisor.py)."""
        masked, pii_count = mask_pii(goal.strip()[: self.max_request_chars])
        tel = Telemetry(provider=self.provider.name, model=self.provider.model, prompt_version=ADVISOR_VERSION,
                        pii_masked=pii_count)
        started = time.perf_counter()
        raw = self._call_model(build_advice_messages(facts, masked, campaign_type), tel, schema=ADVICE_SCHEMA,
                               system=ADVISOR_SYSTEM)
        tel.total_ms = round((time.perf_counter() - started) * 1000, 1)
        self.metrics.incr("advice_calls")
        return raw, tel

    def _call_model(self, messages: list[dict[str, Any]], tel: Telemetry,
                    schema: dict[str, Any] | None = None, system: str | None = None) -> dict[str, Any]:
        if self.budget is not None and not self.budget.available():
            raise BudgetExhausted(f"today's model budget of ${self.budget.limit_usd:.2f} is used up")
        if self.circuit_open:
            raise CircuitOpen("several recent model calls failed; pausing the assistant briefly")
        try:
            result: LLMResult = self.provider.propose(system or self.system_prompt, messages, schema)
        except NotRecorded:
            raise
        except ProviderUnavailable:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.breaker_threshold:
                self._open_until = time.monotonic() + self.breaker_cooldown_s
                log.warning("llm_circuit_opened", extra={"cooldown_s": self.breaker_cooldown_s})
            raise
        self._consecutive_failures = 0
        cost = 0.0 if result.replayed else self.pricing.cost(result)  # a recorded answer costs nothing
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
            "gen_ai.request.max_tokens": max_tokens(schema),
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
        clarifications: list[Clarification] = []
        for name in carried_pending or []:
            term_name, negated = parse_pending(name)
            if term_name in self.catalog.terms:
                self._ask(self.catalog.terms[term_name], negated, clarifications, issues)

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
            phrase = _phrase_in(request_text, term)
            negated = phrase is not None and _negated(request_text, phrase)
            if term.status != "decided":
                self._ask(term, negated, clarifications, issues)
            elif not negated:
                conditions += [Condition(**c, source=f"glossary:{term.name}") for c in term.conditions]
            elif term.option("not"):
                conditions += [Condition(**c, source=f"glossary:{term.name}=not") for c in term.option("not").conditions]
            else:
                issues.append(_cannot_exclude(term))

        self._check_unnamed_terms(request_text, proposal, conditions, clarifications, issues)

        channel = proposal.get("channel")
        if channel in (None, "", "unspecified"):
            channel = self.catalog.default_channel
        if channel not in self.catalog.channels:
            issues.append(Issue(level="warning", code="unknown_channel",
                                message=f"Unknown channel '{channel}', using {self.catalog.default_channel}."))
            channel = self.catalog.default_channel

        definition, problems = validate_definition(AudienceDefinition(conditions=conditions, channel=channel), self.catalog)
        issues += problems
        self._quantify(definition, clarifications, issues)
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

    def _check_unnamed_terms(self, request_text: str, proposal: dict[str, Any], conditions: list[Condition],
                             clarifications: list[Clarification], issues: list[Issue]) -> None:
        """The reverse of the grounding check: the request uses a governed phrase, but the model didn't
        name the term. An open term is asked about anyway, so it can't be guessed. A decided term gets a
        warning saying whether the model wrote its own condition instead, or left it out."""
        named = {str(t) for t in proposal.get("glossary_terms") or []}
        in_use = {c.source.removeprefix("glossary:").partition("=")[0] for c in conditions if c.source.startswith("glossary:")}
        asked = {c.term for c in clarifications}
        model_attributes = {c.attribute for c in conditions if c.source == "model"}
        model_keys = {_condition_key(c.model_dump()) for c in conditions if c.source == "model"}
        for term in self.catalog.terms.values():
            if term.name in named | in_use | asked:
                continue
            phrase = _phrase_in(request_text, term)
            if phrase is None:
                continue
            if term.status == "decided" and {_condition_key(c) for c in term.conditions} <= model_keys:
                continue  # the model wrote exactly the governed definition: same audience, nothing to flag
            if _removed(request_text, phrase):
                continue  # "remove the high value requirement": nothing to apply, nothing to warn about
            overlapping = [c for c in conditions if c.source == "model"
                           and c.attribute in {x["attribute"] for x in term.conditions}]
            if term.status == "decided" and overlapping and all(_in_request(request_text, c.value) for c in overlapping):
                continue  # "more than 45 days", "ARPU over 60 instead": the marketer's own value replaces it
            exclusion = invert(term.conditions) if term.status == "decided" else None
            if exclusion and {_normal(x) for x in exclusion} <= {_normal(c.model_dump()) for c in overlapping}:
                continue  # "don't use the app" written as app_active_30d = false: exactly the exclusion
            if _negated(request_text, phrase):
                if term.status != "decided":
                    # "not active": asked with the exclusion of each definition, never the definitions themselves.
                    self._ask(term, True, clarifications, issues)
                else:
                    issues.append(Issue(level="warning", code="negated_governed_term", message=(
                        f'Your request says "not {phrase}" (governed: {term.rule.rstrip(".")}). Check that the '
                        "conditions below exclude it the way you mean.")))
                continue
            if term.status != "decided":
                clarifications.append(self._clarification(term))
                issues.append(Issue(level="warning", code="unflagged_open_term", message=(
                    f'Your request uses "{phrase}", which has competing definitions, so it is asked about below '
                    "even though the assistant didn't flag it.")))
            elif {c["attribute"] for c in term.conditions} & model_attributes:
                issues.append(Issue(level="warning", code="governed_term_overridden", message=(
                    f'"{phrase}" has a governed definition ({term.rule.rstrip(".")}), but this audience uses its own condition '
                    "instead. Keep it only if that is what you meant.")))
            else:
                issues.append(Issue(level="warning", code="governed_term_missed", message=(
                    f'Your request mentions "{phrase}", but its governed definition ({term.rule.rstrip(".")}) is not applied. '
                    "Add it if you meant it.")))

    def _ask(self, term, negated: bool, clarifications: list[Clarification], issues: list[Issue]) -> None:
        """Ask about an open term; for "not <term>", ask which definition to exclude."""
        clarification = self._clarification(term, negated)
        if clarification is None:
            issues.append(_cannot_exclude(term))
        else:
            clarifications.append(clarification)

    def _clarification(self, term, negated: bool = False) -> Clarification | None:
        default = term.working_default.get("option")
        note = (f"Working default, adopted by {term.working_default['adopted_by']}: {term.working_default['why']} "
                f"The decision itself is with {term.owner_pending}.") if default else None
        options = [term.option(f"{NOT}{o.id}") for o in term.options] if negated else list(term.options)
        if any(o is None for o in options):
            return None
        question = (f'Your request excludes "{term.name.replace("_", " ")}" lines, and that term has competing '
                    "definitions. Which lines should be left out?") if negated else term.question
        chosen_default = f"{NOT}{default}" if negated and default else default
        return Clarification(term=term.name, question=question, working_default_note=note, negated=negated,
                             options=[ClarificationOption(id=o.id, label=o.label, working_default=o.id == chosen_default)
                                      for o in options])

    def _quantify(self, definition: AudienceDefinition, clarifications: list[Clarification], issues: list[Issue]) -> None:
        """Show what each answer would do before it is chosen: reachable lines under each option, with the
        rest of the definition as it is, and how many of them only that option reaches. Skipped while the
        definition has errors, because a size for an invalid audience would mislead."""
        if self.engine is None or not clarifications or any(i.level == "error" for i in issues):
            return
        try:
            for c in clarifications:
                term = self.catalog.terms[c.term]
                alternatives = [definition.model_copy(update={"conditions": [
                    *definition.conditions, *(Condition(**x, source=f"glossary:{term.name}={o.id}") for x in o.conditions)]})
                    for o in (term.option(choice.id) for choice in c.options)]
                for option, (reachable, only_here) in zip(c.options, self.engine.compare(alternatives)):
                    option.reachable, option.only_here = reachable, only_here
        except Exception:
            log.exception("quantify_options_failed")

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
            "clarifications_json": json.dumps([pending_name(c.term, c.negated) for c in result.clarifications]),
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


def _condition_key(c: dict[str, Any]) -> str:
    return json.dumps(canonical(c), sort_keys=True)


_NEGATORS = (r"(?:not(?![ \t]+(?:only|just)\b)|no|non|never|without|excluding|except|stopped|\w+n't|dont|doesnt|"
             r"didnt|isnt|arent|wasnt|werent|havent|hasnt|wont|cant|no longer|sin|excepto)")  # "not only X but Y" is X
_CLAUSE_WORDS = r"(?:and|or|but|plus|y|o|pero)"


def _negated(request_text: str, phrase: str) -> bool:
    """Is the phrase negated within the two words before it, in the same clause? "not active", "don't use
    the app" and "no longer really active" are; "no roaming, active lines" and "not prepaid and active"
    are not (a comma or a conjunction ends the negation)."""
    text = request_text.lower().replace("\u2019", "'")
    pattern = (rf"(?<!\w){_NEGATORS}(?:[ \t]+(?!{_CLAUSE_WORDS}\b)[\w']+){{0,2}}?[ \t]+"
               rf"{re.escape(phrase.lower())}(?!\w)")
    return re.search(pattern, text) is not None


_REMOVERS = r"(?:remove|drop|forget|delete|take out|get rid of|quita|quitar|elimina|eliminar)"


def _removed(request_text: str, phrase: str) -> bool:
    """Does the request ask to remove the phrase ("remove the high value requirement", "forget high value")?"""
    pattern = (rf"(?<!\w){_REMOVERS}(?:[ \t]+(?!{_CLAUSE_WORDS}\b)[\w']+){{0,2}}?[ \t]+"
               rf"{re.escape(phrase.lower())}(?!\w)")
    return re.search(pattern, request_text.lower()) is not None


def _in_request(request_text: str, value: Any) -> bool:
    """Is every value of a condition in the marketer's own words (a number they typed, a value they named)?"""
    text = request_text.lower()
    numbers = {float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}
    for v in value if isinstance(value, list) else [value]:
        if isinstance(v, bool):
            return False
        if isinstance(v, (int, float)) and float(v) not in numbers:
            return False
        if isinstance(v, str) and not re.search(rf"(?<!\w){re.escape(v.lower().replace('_', ' '))}(?!\w)", text):
            return False
    return True


def _normal(c: dict[str, Any]) -> tuple:
    """A condition in a comparable form: "is not true" and "is false" are the same on a yes/no field."""
    if c["op"] in ("eq", "neq") and isinstance(c["value"], bool):
        return (c["attribute"], "eq", c["value"] if c["op"] == "eq" else not c["value"])
    return (c["attribute"], c["op"], json.dumps(c["value"], sort_keys=True))


def _cannot_exclude(term) -> Issue:
    return Issue(level="error", code="negated_term_unsupported", message=(
        f'Your request excludes "{term.name.replace("_", " ")}", but its definition can\'t be excluded with AND-only '
        "conditions. Write the exclusion in the editor."))


def _phrase_in(request_text: str, term) -> str | None:
    """The first of the term's phrases used in the request as whole words ("active", not "inactive")."""
    for phrase in (*term.aliases, term.name.replace("_", " ")):
        if re.search(rf"(?<!\w){re.escape(phrase.lower())}(?!\w)", request_text.lower()):
            return phrase
    return None
