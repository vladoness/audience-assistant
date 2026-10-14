"""HTTP API and static UI.

    POST /api/interpret   plain-English request -> validated proposal (calls the model)
    POST /api/refine      plain-English change to the audience on screen -> validated proposal (calls the model)
    POST /api/check       re-validate after edits or clarification answers (deterministic)
    POST /api/size        audience size, consent exclusions, breakdowns, guardrail warnings
    POST /api/measurement-preview  before approval: holdout size and the smallest lift it would show
    POST /api/approve     server-side re-check + re-size, then an audit record with provenance
    GET  /api/interpretations/{id}  a recorded interpretation, re-checked, for review (e.g. drafted over MCP)
    GET  /api/audiences   approved audiences + whether a governance change affects them (blast radius)
    GET  /api/audiences/{id}/export.csv  the lines to contact (?group=held_out|all for the rest), as approved
    POST /api/audiences/{id}/outcome     the campaign tool reports responders (token); stored as pending
    POST /api/audiences/{id}/outcome/confirm  a person confirms a pending outcome; only then is it learned from
    POST /api/audiences/{id}/simulate-outcome  demo only (synthetic history): generated responses, also pending
    POST /api/advise      the advisor: facts by code, proposals by the model, checked by code (calls the model)
    GET  /api/learning[/{type}]          the loop in numbers, the learning curve per type, expected vs measured
    GET  /api/insights    unmapped-request demand, open-term choices, edit rates (field-to-product signals)
    GET  /api/catalog     attributes, terms and policies for the editor
    GET  /api/metrics     counters and latency percentiles
    GET  /healthz         liveness, plus governance lint receipt, versions, eval summary, budget
    GET  /readyz          readiness: profiles loaded, audit store writable, model path usable (503 if not)
"""

from __future__ import annotations

import hmac
import math
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

import duckdb
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.audience import AudienceEngine
from app.catalog import Catalog, load_catalog
from app.governance import impact, reconcile_with_proposal, reexpand, snapshot
from app.insights import build_insights
from app.interpret import Interpreter, RefineRejected
from app.limits import DailyBudget, RateLimiter
from app.lint import LintReport, lint
from app.llm import AnthropicProvider, Pricing, Provider, ProviderUnavailable, RecordingProvider, ReplayProvider
from app.models import (
    AdviseRequest, ApproveRequest, AudienceDefinition, CheckRequest, CheckResult, ConfirmOutcomeRequest,
    InterpretRequest, InterpretResult, MeasurementPreviewRequest, OutcomeRequest, RefineRequest, SizeResult,
)
from app.observability import Metrics, configure_logging, request_id_var
from app.pii import mask_pii
from app.settings import ROOT, Settings
from app.store import AuditStore, TTLCache, definition_hash
from app import advisor
from app.history import CampaignHistory, counts_lift, verdict
from app.synthdata import CAMPAIGN_TYPES, HISTORY_VERSION, add_history, generate

log = logging.getLogger("app")


@dataclass
class Services:
    settings: Settings
    catalog: Catalog
    engine: AudienceEngine
    store: AuditStore
    interpreter: Interpreter
    metrics: Metrics
    limiter: RateLimiter
    budget: DailyBudget
    lint_report: LintReport
    eval_summary: dict | None
    history: CampaignHistory | None = None
    today: Callable[[], date] = date.today


def build_provider(settings: Settings) -> Provider:
    if settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY")
        provider: Provider = AnthropicProvider(settings.anthropic_api_key, settings.anthropic_model,
                                               settings.llm_timeout_s, settings.llm_max_retries,
                                               effort=settings.anthropic_effort or None)
        return RecordingProvider(provider, settings.fixtures_dir) if settings.record_fixtures else provider
    return ReplayProvider(settings.fixtures_dir, model=settings.anthropic_model, effort=settings.anthropic_effort or None)


def load_eval_summary(model: str, effort: str | None) -> dict | None:
    """Measured accuracy for the active model + effort, from the committed eval results (dev, held-out, refine)."""
    out = {}
    for case_set in ("dev", "heldout", "refine"):
        path = ROOT / "evals" / "results" / f"{model}@{effort or 'default'}.{case_set}.json"
        if path.exists():
            summary = json.loads(path.read_text())["summary"]
            out[case_set] = {k: summary.get(k) for k in ("cases", "runs", "pass_rate", "latency_p50_ms")}
    return out or None


def build_services(settings: Settings, provider: Provider | None = None) -> Services:
    catalog = load_catalog(settings.config_dir)
    report = lint(catalog)
    for warning in report.warnings:
        log.warning("governance_lint_warning", extra={"detail": warning})
    if report.errors:
        raise RuntimeError(f"{report.receipt}: " + "; ".join(report.errors))
    log.info("governance_lint", extra={"receipt": report.receipt, "versions": catalog.versions})

    if not settings.data_path.exists():
        if not settings.synthetic_data:
            raise RuntimeError(f"No profile store at {settings.data_path}. Run `make data`, or set SYNTHETIC_DATA=1 "
                               "to generate the synthetic demo store at startup.")
        log.warning("generating_synthetic_data", extra={"rows": settings.profile_rows, "path": str(settings.data_path)})
        generate(settings.data_path, settings.profile_rows)
    elif settings.synthetic_data and _history_version(settings.data_path) != HISTORY_VERSION:
        log.warning("adding_synthetic_campaign_history", extra={"path": str(settings.data_path), "version": HISTORY_VERSION})
        add_history(settings.data_path)
    guardrails = catalog.parameter_sets.get("audience_guardrails")
    min_audience = int(guardrails.values["min_reachable_lines"]) if guardrails else settings.min_audience
    broad_share = float(guardrails.values["max_share_of_base"]) if guardrails else settings.broad_share
    engine = AudienceEngine(settings.data_path, catalog, min_audience, broad_share)
    store = AuditStore(settings.audit_db_path)
    metrics = Metrics()
    budget = DailyBudget(settings.daily_budget_usd)
    interpreter = Interpreter(
        catalog=catalog,
        provider=provider or build_provider(settings),
        store=store,
        cache=TTLCache(settings.cache_ttl_s, settings.cache_max_entries),
        metrics=metrics,
        pricing=Pricing.load(settings.config_dir / "models.yaml"),
        max_request_chars=settings.max_request_chars,
        budget=budget,
        breaker_threshold=settings.breaker_threshold,
        breaker_cooldown_s=settings.breaker_cooldown_s,
        engine=engine,
    )
    history = CampaignHistory(engine, catalog)
    log.info("startup", extra={"provider": interpreter.provider.name, "model": interpreter.provider.model,
                               "profiles": engine.base, "catalog_version": catalog.version,
                               "campaign_history": "synthetic" if history.synthetic else history.available})
    return Services(settings, catalog, engine, store, interpreter, metrics,
                    limiter=RateLimiter(settings.rate_limit_per_min), budget=budget, lint_report=report,
                    eval_summary=load_eval_summary(interpreter.provider.model, getattr(interpreter.provider, "effort", None)),
                    history=history)


def _history_version(path) -> int | None:
    """Version of the synthetic campaign history in the store, None if there is none (or it predates versions)."""
    with duckdb.connect(str(path), read_only=True) as con:
        if "history_meta" not in {r[0] for r in con.execute("SHOW TABLES").fetchall()}:
            return None
        columns = {r[0] for r in con.execute("DESCRIBE history_meta").fetchall()}
        return con.execute("SELECT version FROM history_meta").fetchone()[0] if "version" in columns else None


def create_app(settings: Settings | None = None, provider: Provider | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = build_services(settings, provider)
        yield

    app = FastAPI(title="Audience Assistant", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                log.exception("unhandled_error", extra={"method": request.method, "path": request.url.path})
                response = JSONResponse(status_code=500, content={"detail": "Internal error", "request_id": rid})
            elapsed = (time.perf_counter() - started) * 1000
            response.headers["x-request-id"] = rid
            if request.url.path.startswith("/api"):
                metrics = app.state.services.metrics
                log.info("http_request", extra={"method": request.method, "path": request.url.path,
                                                "status_code": response.status_code, "duration_ms": round(elapsed, 1),
                                                "upstream_request_id": request.headers.get("x-request-id")})
                metrics.observe(f"http {request.method} {request.url.path}", elapsed)
                metrics.incr(f"http_{response.status_code // 100}xx")
            return response
        finally:
            request_id_var.reset(token)

    def svc(request: Request) -> Services:
        return request.app.state.services

    def rate_limit(s: Services, request: Request) -> None:
        allowed, retry_after = s.limiter.allow(request.client.host if request.client else "unknown")
        if not allowed:
            s.metrics.incr("rate_limited")
            raise HTTPException(429, detail="Too many requests to the assistant; try again shortly.",
                                headers={"Retry-After": str(max(1, round(retry_after)))})

    @app.post("/api/interpret", response_model=InterpretResult)
    def interpret(body: InterpretRequest, request: Request) -> InterpretResult:
        s = svc(request)
        rate_limit(s, request)
        return s.interpreter.interpret(body.request, request_id=request_id_var.get())

    @app.post("/api/refine", response_model=InterpretResult)
    def refine(body: RefineRequest, request: Request) -> InterpretResult:
        s = svc(request)
        rate_limit(s, request)
        parent = s.store.get_interpretation(body.request_id)
        if parent is None:
            raise HTTPException(404, detail="Unknown request_id: interpret the request first.")
        try:
            return s.interpreter.refine(parent, body.definition, body.instruction, request_id=request_id_var.get())
        except RefineRejected as exc:
            raise HTTPException(422, detail=str(exc)) from None
        except ProviderUnavailable as exc:
            raise HTTPException(503, detail=f"The assistant is unavailable ({exc}). Your audience is unchanged; "
                                            "edit it below.") from None

    @app.post("/api/check", response_model=CheckResult)
    def check(body: CheckRequest, request: Request) -> CheckResult:
        return svc(request).interpreter.check(body.definition, body.pending_terms, body.resolutions)

    @app.post("/api/size", response_model=SizeResult)
    def size(definition: AudienceDefinition, request: Request) -> SizeResult:
        s = svc(request)
        checked = s.interpreter.check(definition, [], {})
        if checked.status == "invalid":
            raise HTTPException(422, detail=[i.model_dump() for i in checked.issues if i.level == "error"])
        result = s.engine.size(checked.definition)
        s.metrics.observe("size_query_ms", result.query_ms)
        if s.history is not None:
            result.contact_pressure = s.history.contact_pressure(checked.definition)
        return result

    @app.post("/api/advise")
    def advise(body: AdviseRequest, request: Request) -> dict:
        """The campaign advisor: the model proposes up to three improvements from facts the system computed;
        each is checked against those facts and sized before the marketer sees it."""
        s = svc(request)
        rate_limit(s, request)
        return run_advice(s, body.definition, body.goal, body.campaign_type)

    @app.get("/api/history")
    def history(request: Request) -> dict:
        h = svc(request).history
        if h is None or not h.available:
            return {"available": False, "types": []}
        return {"available": True, "synthetic": h.synthetic, "as_of": str(h.as_of), "types": h.types()}

    @app.get("/api/history/evidence/{campaign_type}")
    def evidence(campaign_type: str, request: Request) -> dict:
        h = svc(request).history
        found = h.evidence(campaign_type, svc(request).store.outcomes()) if h is not None else None
        if found is None:
            raise HTTPException(404, detail=f"No campaign history for type {campaign_type!r}.")
        return found

    @app.post("/api/measurement-preview")
    def measurement_preview(body: MeasurementPreviewRequest, request: Request) -> dict:
        """Before approval: how big the holdout would be, and whether a lift like past campaigns' would show."""
        s = svc(request)
        checked = s.interpreter.check(body.definition, [], {})
        if checked.status == "invalid" or s.history is None or body.campaign_type not in CAMPAIGN_TYPES:
            return {"available": False}
        ps = s.catalog.parameter_sets.get("measurement")
        share = float(ps.values["holdout_share"]) if ps else 0.0
        reachable = s.engine.size(checked.definition).reachable
        holdout = round(reachable * share)  # the expected split: the real one is drawn at approval
        target = reachable - holdout
        past = s.history.evidence(body.campaign_type, s.store.outcomes())
        detectable, note = detectable_lift(past, target, holdout)
        return {"available": True, "target_lines": target, "holdout_lines": holdout, "past_lift_points":
                past["pooled"]["lift_points"], "detectable_lift_points": detectable, "power_note": note}

    @app.post("/api/approve")
    def approve(body: ApproveRequest, request: Request) -> dict:
        s = svc(request)
        interpretation = s.store.get_interpretation(body.request_id)
        if interpretation is None:
            raise HTTPException(404, detail="Unknown request_id: interpret the request first.")
        existing = s.store.approval_for(body.request_id)
        if existing is not None:
            raise HTTPException(409, detail=f"This request was already approved as audience #{existing}.")
        checked = s.interpreter.check(body.definition, [], {})
        if checked.status != "ready":
            raise HTTPException(422, detail={"status": checked.status, "issues": [i.model_dump() for i in checked.issues]})
        if not checked.definition.conditions:
            raise HTTPException(422, detail={"status": "invalid", "detail": "An audience needs at least one condition."})
        answered = {c.source.removeprefix("glossary:").partition("=")[0]
                    for c in checked.definition.conditions if c.source.startswith("glossary:") and "=" in c.source}
        unanswered = [t for t in json.loads(interpretation["clarifications_json"]) if t not in answered]
        if unanswered:
            raise HTTPException(422, detail={"status": "needs_clarification", "unanswered_terms": unanswered})
        unmapped = json.loads(interpretation["unmapped_json"])
        if unmapped and not body.acknowledged_unmapped:
            raise HTTPException(422, detail={"status": "needs_confirmation", "unmapped_phrases": unmapped})
        known_types = {t["id"] for t in s.history.types()} if s.history is not None else set()
        if body.campaign_type is not None and body.campaign_type not in known_types:
            raise HTTPException(422, detail=f"Unknown campaign type {body.campaign_type!r}.")

        proposed = AudienceDefinition(**json.loads(interpretation["definition_json"]))
        final, edited = reconcile_with_proposal(checked.definition, proposed)
        sized = s.engine.size(final)
        measurement = measurement_plan(s, final, body.goal.strip(), salt=body.request_id,
                                       campaign_type=body.campaign_type)
        members = s.engine.members(final, body.request_id, measurement["holdout_share"])
        definition = final.model_dump()
        audience_id = s.store.record_approval_once(members=members, row={
            "name": body.name.strip(),
            "approver": body.approver.strip(),
            "request_id": body.request_id,
            "definition_json": json.dumps(definition),
            "definition_hash": definition_hash(definition),
            "edited_by_user": int(edited),
            "explanation": checked.explanation.summary,
            "channel": final.channel,
            "matched": sized.matched,
            "reachable": sized.reachable,
            "acknowledged_unmapped_json": json.dumps(unmapped if body.acknowledged_unmapped else []),
            "governance_json": json.dumps(snapshot(final, s.catalog)),
            "governance_versions": json.dumps(s.catalog.versions),
            "measurement_json": json.dumps(measurement),
            "client_json": json.dumps(_client(request)),
        })
        s.metrics.incr("audiences_approved")
        if audience_id is None:
            raise HTTPException(409, detail="This request was just approved by someone else.")
        log.info("audience_approved", extra={"audience_id": audience_id, "reachable": sized.reachable,
                                             "definition_hash": definition_hash(definition),
                                             "interpretation_request_id": body.request_id, "edited_by_user": edited,
                                             "holdout_lines": measurement["holdout_lines"]})
        return {"id": audience_id, "reachable": sized.reachable, "definition_hash": definition_hash(definition),
                "measurement": measurement}

    @app.get("/api/audiences")
    def audiences(request: Request) -> list[dict]:
        s = svc(request)
        rows = []
        outcomes = {o["audience_id"]: o for o in s.store.outcomes(include_pending=True)}
        for row in s.store.list_audiences():
            o = outcomes.get(row["id"])
            row["outcome"] = None if o is None else outcome_view(s, o, row["measurement_json"])
            definition = AudienceDefinition(**json.loads(row["definition_json"]))
            changes = impact(json.loads(row["governance_json"]), s.catalog)
            row["governance_changes"] = changes
            try:
                row["reachable_now"] = s.engine.size(definition).reachable
                row["reachable_under_current_rules"] = (
                    s.engine.size(reexpand(definition, s.catalog)).reachable if changes else row["reachable_now"])
            except ValueError as exc:
                row["governance_changes"] = changes + [f"no longer valid under the current catalog ({exc})"]
                row["reachable_now"] = row["reachable_under_current_rules"] = None
            approved_snapshot = json.loads(row["measurement_json"]).get("data_snapshot")
            row["data_changed_since_approval"] = approved_snapshot is not None and approved_snapshot != s.engine.snapshot
            rows.append(row)
        return rows

    @app.post("/api/audiences/{audience_id}/outcome")
    def outcome(audience_id: int, body: OutcomeRequest, request: Request) -> dict:
        """Close the loop: the campaign tool reports who responded, with the shared outcome token. The lift
        is measured against the membership recorded at approval and stored as pending: it joins the
        evidence only when a person confirms it. A new report replaces a pending one."""
        s = svc(request)
        if not s.settings.outcome_token:
            raise HTTPException(403, detail="Outcome reporting is off: set OUTCOME_TOKEN and send it as X-Outcome-Token.")
        if not hmac.compare_digest(request.headers.get("x-outcome-token", ""), s.settings.outcome_token):
            raise HTTPException(401, detail="Missing or wrong X-Outcome-Token.")
        return record_outcome(s, audience_id, body.responders, source="campaign_tool",
                              sent_at=body.sent_at or s.today().isoformat(), reporter=_client(request))

    @app.post("/api/audiences/{audience_id}/outcome/confirm")
    def confirm_outcome(audience_id: int, body: ConfirmOutcomeRequest, request: Request) -> dict:
        """A person checked the reported numbers. Only confirmed outcomes are learned from."""
        s = svc(request)
        if not s.store.confirm_outcome(audience_id, body.confirmed_by.strip()):
            raise HTTPException(409, detail="There is no pending outcome for this audience to confirm.")
        s.metrics.incr("outcomes_confirmed")
        log.info("outcome_confirmed", extra={"audience_id": audience_id})
        return {"audience_id": audience_id, "status": "confirmed"}

    @app.post("/api/audiences/{audience_id}/simulate-outcome")
    def simulate_outcome(audience_id: int, request: Request) -> dict:
        """DEMO ONLY, with the synthetic history: generate the responses a send would have produced,
        from the same known effects, and record them through the same path as a real report (pending
        until confirmed). The send date is today and the measurement window is not waited for."""
        s = svc(request)
        if s.history is None or not s.history.synthetic:
            raise HTTPException(409, detail="Simulated outcomes are only available with the synthetic demo history.")
        row = s.store.get_audience(audience_id)
        if row is None:
            raise HTTPException(404, detail="Unknown audience.")
        m = json.loads(row["measurement_json"])
        if not m.get("campaign_type"):
            raise HTTPException(422, detail="This audience has no campaign type, so there is nothing to simulate against.")
        responders = s.history.simulate_responders(s.store.members(audience_id), m["campaign_type"],
                                                   seed=f"outcome:{audience_id}")
        return record_outcome(s, audience_id, responders, source="simulated", sent_at=s.today().isoformat(),
                              reporter=_client(request))

    @app.get("/api/learning")
    def learning_summary(request: Request) -> dict:
        """The loop in numbers: how many decisions were defined, approved, measured, and what was learned.
        Evidence uses confirmed outcomes only; the scorecard shows pending ones too, marked."""
        s = svc(request)
        own = s.store.outcomes()
        types = []
        for t in s.history.types() if s.history is not None else []:
            now = s.history.evidence(t["id"], own)
            before = s.history.evidence(t["id"])
            types.append({"id": t["id"], "label": t["label"], "campaigns": now["campaigns"],
                          "measured_here": now["measured_here"], "estimate_now": now["pooled"],
                          "estimate_from_history_only": before["pooled"]})
        scorecard = [{"audience_id": o["audience_id"], "name": o["name"], "campaign_type": o["campaign_type"],
                      **outcome_view(s, o, o["measurement_json"])}
                     for o in s.store.outcomes(include_pending=True)]
        return {"synthetic": bool(s.history and s.history.synthetic), "loop": s.store.counts(),
                "types": types, "scorecard": scorecard}

    @app.get("/api/learning/{campaign_type}")
    def learning_curve(campaign_type: str, request: Request) -> dict:
        s = svc(request)
        found = s.history.learning(campaign_type, s.store.outcomes()) if s.history is not None else None
        if found is None:
            raise HTTPException(404, detail=f"No campaign history for type {campaign_type!r}.")
        return found

    @app.get("/api/audiences/{audience_id}/export.csv")
    def export(audience_id: int, request: Request, group: Literal["contacted", "held_out", "all"] = "contacted") -> Response:
        """The approved audience for the campaign tool, exactly as approved (the membership recorded at
        approval, not a re-run of the definition). By default only the lines to contact, so the holdout
        can't be sent to by mistake; ?group=held_out or all for the measurement side. A header says whether
        today's data would still produce the same groups."""
        s = svc(request)
        row = s.store.get_audience(audience_id)
        if row is None:
            raise HTTPException(404, detail="Unknown audience.")
        current = _current_data_matches(s, row)
        members = s.store.members(audience_id, None if group == "all" else group)
        lines = ["profile_id,group", *(f"{pid},{grp}" for pid, grp in members)]
        s.metrics.incr("audience_exports")
        log.info("audience_exported", extra={"audience_id": audience_id, "lines": len(members), "group": group,
                                             "current_data_matches_approval": current})
        m = json.loads(row["measurement_json"])
        return Response("\n".join(lines) + "\n", media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="audience-{audience_id}-{group}.csv"',
            "X-Membership": "as approved",
            "X-Current-Data-Matches-Approval": "true" if current else "false",
            "X-Approved-Data-Snapshot": str(m.get("data_snapshot", "")),
            "X-Current-Data-Snapshot": s.engine.snapshot,
        })

    @app.get("/api/insights")
    def insights(request: Request) -> dict:
        s = svc(request)
        return build_insights(s.store, s.catalog)

    @app.get("/api/interpretations/{request_id}", response_model=InterpretResult)
    def interpretation(request_id: str, request: Request) -> InterpretResult:
        """A recorded interpretation, re-checked, for review and approval (e.g. drafted by an agent over MCP)."""
        s = svc(request)
        row = s.store.get_interpretation(request_id)
        if row is None:
            raise HTTPException(404, detail="Unknown request_id.")
        return s.interpreter.review(row)

    @app.get("/api/catalog")
    def catalog(request: Request) -> dict:
        return catalog_view(svc(request).catalog) | {"examples": EXAMPLES, "refine_examples": REFINE_EXAMPLES}

    @app.get("/api/metrics")
    def metrics(request: Request) -> dict:
        return svc(request).metrics.snapshot()

    @app.get("/readyz")
    def readyz(request: Request) -> JSONResponse:
        s = svc(request)
        checks = {
            "profiles_loaded": s.engine.base > 0,
            "audit_store_writable": _safe(s.store.can_write),
            "model_path_usable": not s.interpreter.circuit_open and s.budget.available(),
        }
        ready = checks["profiles_loaded"] and checks["audit_store_writable"]
        degraded = [] if checks["model_path_usable"] else ["assistant paused (budget or repeated model failures); manual editor works"]
        return JSONResponse(status_code=200 if ready else 503, content={"ready": ready, "checks": checks, "degraded": degraded})

    @app.get("/healthz")
    def healthz(request: Request) -> dict:
        s = svc(request)
        return {"status": "ok", "profiles": s.engine.base, "provider": s.interpreter.provider.name,
                "model": s.interpreter.provider.model, "governance": s.catalog.versions,
                "governance_lint": s.lint_report.receipt, "eval": s.eval_summary,
                "model_spend_today_usd": s.budget.spent_today, "model_budget_usd": s.budget.limit_usd}

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(ROOT / "static" / "index.html")

    return app


EXAMPLES = [
    "Prepaid customers in the Coast region who haven't topped up in over 30 days but still use the app",
    "High value postpaid customers who roamed abroad last month, for an email campaign",
    "Active customers on Android with more than 10 GB of data in the last 30 days",
    "Customers likely to churn in the Capital",
    "New customers on premium plans who called the call centre twice",
    "Postpaid customers with a high credit risk band",
]
REFINE_EXAMPLES = ["also include the Highlands", "only postpaid", "make it SMS instead"]


def run_advice(s: Services, definition: AudienceDefinition, goal: str, campaign_type: str | None) -> dict:
    """Facts by code, proposals by the model, checks and consequences by code. Shared by the API and MCP."""
    checked = s.interpreter.check(definition, [], {})
    if checked.status == "invalid":
        raise HTTPException(422, detail="Fix the audience's errors before asking for advice.")
    if campaign_type is not None and campaign_type not in CAMPAIGN_TYPES:
        raise HTTPException(422, detail=f"Unknown campaign type {campaign_type!r}.")
    facts = advisor.fact_sheet(s.engine, s.catalog, s.history, s.store, checked.definition, campaign_type)
    try:
        raw, tel = s.interpreter.advise(facts, goal, campaign_type)
    except ProviderUnavailable as exc:
        raise HTTPException(503, detail=f"The advisor is unavailable ({exc}).") from None
    by_id = {f["id"]: f for f in facts}
    holdout = s.catalog.parameter_sets.get("measurement")
    share = float(holdout.values["holdout_share"]) if holdout else 0.1
    accepted, rejected = [], []
    for p in (raw.get("proposals") or [])[:3]:
        reason = advisor.check(p, by_id, s.catalog, checked.definition, campaign_type)
        if reason:
            rejected.append({"title": str(p.get("title", ""))[:200], "kind": p.get("kind"), "reason": reason})
        else:
            accepted.append({k: p[k] for k in ("kind", "title", "rationale", "cites", "attribute", "value", "channel",
                                               "campaign_type")}
                            | {"cited_facts": [by_id[c]["text"] for c in p["cites"]],
                               "consequence": advisor.consequence(p, by_id, s.engine, s.catalog, checked.definition,
                                                                  campaign_type, holdout_share=share)})
    summary = str(raw.get("summary", ""))[:600]
    summary_problem = advisor.check_summary(summary, by_id)
    if summary_problem:
        rejected.append({"title": "Summary", "kind": "summary", "reason": summary_problem})
        summary = ""
    s.metrics.incr("advice_accepted", len(accepted))
    s.metrics.incr("advice_rejected", len(rejected))
    advice_id = s.store.record_advice({
        "campaign_type": campaign_type, "goal_masked": mask_pii(goal.strip()[: s.settings.max_request_chars])[0],
        "facts_json": json.dumps([{"id": f["id"], "text": f["text"]} for f in facts]), "raw_json": json.dumps(raw),
        "accepted_json": json.dumps(accepted), "rejected_json": json.dumps(rejected),
        "model": tel.model, "prompt_version": tel.prompt_version})
    log.info("advice", extra={"advice_id": advice_id, "accepted": [p["kind"] for p in accepted],
                              "rejected": [r["reason"] for r in rejected], "facts": len(facts),
                              "campaign_type": campaign_type})
    return {"advice_id": advice_id, "summary": summary, "proposals": accepted, "rejected": rejected,
            "facts": [{"id": f["id"], "text": f["text"]} for f in facts], "telemetry": tel.model_dump(),
            "synthetic_history": bool(s.history and s.history.synthetic),
            "model_written": ["summary", "proposals[].title", "proposals[].rationale"]}


def record_outcome(s: Services, audience_id: int, responders: list[str], source: str, sent_at: str,
                   reporter: dict) -> dict:
    """Count responders in the contacted and held-out groups recorded at approval, measure the lift, and
    store it as pending. A report that can't be right is rejected before it can reach the evidence: sent
    before the approval or in the future, still inside the measurement window, mostly lines that aren't in
    this audience, or a response rate no campaign has."""
    row = s.store.get_audience(audience_id)
    if row is None:
        raise HTTPException(404, detail="Unknown audience.")
    m = json.loads(row["measurement_json"])
    if "split" not in m:
        raise HTTPException(422, detail="This audience was approved without a holdout, so its lift can't be measured.")
    ps = s.catalog.parameter_sets.get("measurement")
    max_rate = float(ps.values.get("max_response_rate", 1.0)) if ps else 1.0
    approved_on = datetime.fromisoformat(row["created_at"]).astimezone().date()  # stored in UTC; compared locally
    today, sent = s.today(), date.fromisoformat(sent_at)
    if not approved_on <= sent <= today:
        raise HTTPException(422, detail=f"sent_at {sent_at} must be between the approval ({approved_on}) and today.")
    if source == "campaign_tool" and today < sent + timedelta(days=m["window_days"]):
        raise HTTPException(422, detail=(f"The {m['window_days']}-day measurement window is open until "
                                         f"{sent + timedelta(days=m['window_days'])}; report after that."))
    groups = dict(s.store.members(audience_id))
    responded = set(responders)
    contacted = sum(1 for g in groups.values() if g == "contacted")
    held_out = len(groups) - contacted
    contacted_r = sum(1 for p in responded if groups.get(p) == "contacted")
    held_out_r = sum(1 for p in responded if groups.get(p) == "held_out")
    unknown = len(responded) - contacted_r - held_out_r
    if responded and unknown > len(responded) / 2:
        raise HTTPException(422, detail=(f"{unknown:,} of {len(responded):,} reported responders are not in this "
                                         "audience: is this the right file?"))
    for name, r, n in (("contacted", contacted_r, contacted), ("held-out", held_out_r, held_out)):
        if n and r / n > max_rate:
            raise HTTPException(422, detail=(f"{r / n:.0%} of the {name} lines responding is above the plausible "
                                             f"maximum ({max_rate:.0%}, measurement v{ps.version})."))
    stored = s.store.report_outcome({
        "audience_id": audience_id, "source": source, "campaign_type": m.get("campaign_type"), "sent_at": sent_at,
        "contacted": contacted, "contacted_responders": contacted_r, "held_out": held_out,
        "held_out_responders": held_out_r, "unknown_responders": unknown,
        "membership_matches": int(_current_data_matches(s, row)), "reporter_json": json.dumps(reporter),
    })
    if not stored:
        raise HTTPException(409, detail="This audience's outcome is already confirmed; it can't be replaced.")
    s.metrics.incr(f"outcomes_{source}")
    result = {"audience_id": audience_id, "campaign_type": m.get("campaign_type"),
              **outcome_view(s, s.store.outcomes(include_pending=True, audience_id=audience_id)[0], row["measurement_json"])}
    log.info("outcome_reported", extra={k: result[k] for k in ("audience_id", "source", "campaign_type", "verdict")}
             | {"lift_points": result["measured"]["lift_points"], "unknown_responders": unknown})
    return result


def outcome_view(s: Services, o: dict, measurement_json: str) -> dict:
    """A stored outcome as the UI and API show it: measured against what was expected at approval."""
    expected = json.loads(measurement_json).get("evidence_at_approval")
    ps = s.catalog.parameter_sets.get("measurement")
    measured = counts_lift(o["contacted"], o["held_out"], o["contacted_responders"], o["held_out_responders"])
    return {"source": o["source"], "sent_at": o["sent_at"], "status": o["status"], "reports": o["reports"],
            "confirmed_by": o["confirmed_by"], "measured": measured.as_dict(), "expected": expected,
            "verdict": verdict(expected, measured, int(ps.values["min_holdout_lines"]) if ps else 0),
            "unknown_responders": o["unknown_responders"],
            "current_data_matches_approval": bool(o["membership_matches"])}


def _current_data_matches(s: Services, row: dict) -> bool:
    """Would today's profile data still put exactly the same lines in each group?"""
    m = json.loads(row["measurement_json"])
    if "split" not in m:
        return False
    definition = AudienceDefinition(**json.loads(row["definition_json"]))
    return m.get("members_sha") == s.engine.member_digests(definition, m["split"]["salt"], m["holdout_share"])


def _client(request: Request) -> dict:
    """Who called, as far as the server can tell. Without SSO a name is self-declared; this at least
    records where a call came from (an agent calling the API directly shows up here)."""
    return {"host": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent", "")[:200]}


def measurement_plan(s: Services, definition: AudienceDefinition, goal: str, salt: str,
                     campaign_type: str | None = None) -> dict:
    """How this decision will be judged later: the campaign goal, a governed share of the audience held out
    (not contacted), and the comparison that says whether it worked. Recorded with the approval, together
    with the evidence the approver had: past lift for this campaign type and the audience's contact pressure."""
    ps = s.catalog.parameter_sets.get("measurement")
    share = float(ps.values["holdout_share"]) if ps else 0.0
    window = int(ps.values["window_days"]) if ps else 0
    target, holdout = s.engine.split(definition, salt, share)
    measurable = ps is not None and holdout >= int(ps.values["min_holdout_lines"])
    past = s.history.evidence(campaign_type, s.store.outcomes()) if s.history is not None and campaign_type else None
    pressure = s.history.contact_pressure(definition) if s.history is not None else None
    detectable, power_note = detectable_lift(past, target, holdout)
    return {
        "goal": goal,
        "campaign_type": campaign_type,
        "evidence_at_approval": None if past is None else {
            "synthetic": past["synthetic"], "as_of": past["as_of"], "campaigns": past["campaigns"], **past["pooled"]},
        "contact_pressure_at_approval": None if pressure is None else pressure.model_dump(exclude={"warnings"}),
        "success_measure": (f"Response rate of the {target:,} contacted lines minus that of the {holdout:,} held-out "
                            f"lines, over {window} days after the send"
                            + (f" (a response: the line {CAMPAIGN_TYPES[campaign_type]['response']})."
                               if campaign_type else ".")),
        "target_lines": target,
        "holdout_lines": holdout,
        "holdout_share": share,
        "window_days": window,
        "measurable": measurable,
        "note": None if measurable else "The holdout is too small for the result to be reliable; widen the audience "
                                        "or treat the outcome as anecdotal.",
        "detectable_lift_points": detectable,
        "power_note": power_note,
        "split": {"method": "md5_number_lower(salt || ':' || profile_id) % 10000 < share x 10000", "salt": salt},
        "parameters": f"measurement v{ps.version}" if ps else None,
        "data_snapshot": s.engine.snapshot,
        "members_sha": s.engine.member_digests(definition, salt, share),
    }


def detectable_lift(past: dict | None, target: int, holdout: int) -> tuple[float | None, str | None]:
    """The smallest lift this split would show clearly (80% of the time, at 95%: z 1.96 + 0.84), at the
    campaign type's past base rate, and a warning when past campaigns of the type lifted less than that."""
    if past is None or not target or not holdout:
        return None, None
    p = past["pooled"]["held_out_response_pct"] / 100
    detectable = round(100 * 2.8 * math.sqrt(p * (1 - p) * (1 / target + 1 / holdout)), 2)
    if past["pooled"]["lift_points"] >= detectable:
        return detectable, None
    return detectable, (f"With {holdout:,} lines held out, only a lift of about {detectable:.2f} points or more would "
                        f"show clearly; past campaigns of this type lifted {past['pooled']['lift_points']:+.2f}, so this "
                        "one will probably read \"too small to tell\". A wider audience or a larger holdout share "
                        "would fix it.")


def catalog_view(c: Catalog) -> dict:
    """Fields, governed terms, policies and channels: what the editor and an MCP client need to know."""
    return {
        "version": c.version,
        "attributes": [
            {"name": a.name, "label": a.label, "type": a.type, "values": list(a.values), "min": a.min,
             "max": a.max, "unit": a.unit, "description": a.description}
            for a in c.attributes.values() if c.targetable_somewhere(a.name)
        ],
        "terms": {
            t.name: {"status": t.status, "rule": t.rule, "decision_id": t.decision.get("id"),
                     "owner": t.decision.get("owner"),
                     "parameters": {n: c.parameter_sets[n].version for n in t.parameter_sets}}
            for t in c.terms.values()
        },
        "policies": [{"id": p.id, "name": p.name, "deny_labels": list(p.deny_labels),
                      "marketing_actions": list(p.marketing_actions)} for p in c.policies],
        "channels": list(c.channels),
    }


def run() -> None:
    import uvicorn

    configure_logging()
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port, log_config=None, access_log=False, proxy_headers=False)


def _safe(check) -> bool:
    try:
        return bool(check())
    except Exception:
        return False


if __name__ == "__main__":
    run()
