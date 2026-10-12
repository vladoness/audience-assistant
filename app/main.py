"""HTTP API and static UI.

    POST /api/interpret   plain-English request -> validated proposal (the only endpoint that calls the model)
    POST /api/check       re-validate after edits or clarification answers (deterministic)
    POST /api/size        audience size, consent exclusions, breakdowns, guardrail warnings
    POST /api/approve     server-side re-check + re-size, then an audit record with provenance
    GET  /api/audiences   approved audiences + whether a governance change affects them (blast radius)
    GET  /api/insights    unmapped-request demand, open-term choices, edit rates (field-to-product signals)
    GET  /api/catalog     attributes, terms and policies for the editor
    GET  /api/metrics     counters and latency percentiles
    GET  /healthz         liveness, plus governance lint receipt, versions, eval summary, budget
    GET  /readyz          readiness: profiles loaded, audit store writable, model path usable (503 if not)
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.audience import AudienceEngine
from app.catalog import Catalog, load_catalog
from app.governance import impact, reconcile_with_proposal, reexpand, snapshot
from app.insights import build_insights
from app.interpret import Interpreter
from app.limits import DailyBudget, RateLimiter
from app.lint import LintReport, lint
from app.llm import AnthropicProvider, Pricing, Provider, RecordingProvider, ReplayProvider
from app.models import (
    ApproveRequest, AudienceDefinition, CheckRequest, CheckResult, InterpretRequest, InterpretResult, SizeResult,
)
from app.observability import Metrics, configure_logging, request_id_var
from app.settings import ROOT, Settings
from app.store import AuditStore, TTLCache, definition_hash
from app.synthdata import generate

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
    path = ROOT / "evals" / "results" / f"{model}@{effort or 'default'}.dev.json"
    if not path.exists():
        return None
    summary = json.loads(path.read_text())["summary"]
    return {k: summary.get(k) for k in ("model", "effort", "cases", "runs", "pass_rate", "audience_exact_match",
                                        "latency_p50_ms", "case_set")}


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
    )
    log.info("startup", extra={"provider": interpreter.provider.name, "model": interpreter.provider.model,
                               "profiles": engine.base, "catalog_version": catalog.version})
    return Services(settings, catalog, engine, store, interpreter, metrics,
                    limiter=RateLimiter(settings.rate_limit_per_min), budget=budget, lint_report=report,
                    eval_summary=load_eval_summary(interpreter.provider.model, getattr(interpreter.provider, "effort", None)))


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

    @app.post("/api/interpret", response_model=InterpretResult)
    def interpret(body: InterpretRequest, request: Request) -> InterpretResult:
        s = svc(request)
        allowed, retry_after = s.limiter.allow(request.client.host if request.client else "unknown")
        if not allowed:
            s.metrics.incr("rate_limited")
            raise HTTPException(429, detail="Too many requests to the assistant; try again shortly.",
                                headers={"Retry-After": str(max(1, round(retry_after)))})
        return s.interpreter.interpret(body.request, request_id=request_id_var.get())

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
        return result

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
        answered = {c.source.removeprefix("glossary:").partition("=")[0]
                    for c in checked.definition.conditions if c.source.startswith("glossary:") and "=" in c.source}
        unanswered = [t for t in json.loads(interpretation["clarifications_json"]) if t not in answered]
        if unanswered:
            raise HTTPException(422, detail={"status": "needs_clarification", "unanswered_terms": unanswered})
        unmapped = json.loads(interpretation["unmapped_json"])
        if unmapped and not body.acknowledged_unmapped:
            raise HTTPException(422, detail={"status": "needs_confirmation", "unmapped_phrases": unmapped})

        proposed = AudienceDefinition(**json.loads(interpretation["definition_json"]))
        final, edited = reconcile_with_proposal(checked.definition, proposed)
        sized = s.engine.size(final)
        definition = final.model_dump()
        audience_id = s.store.record_approval({
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
        })
        s.metrics.incr("audiences_approved")
        log.info("audience_approved", extra={"audience_id": audience_id, "reachable": sized.reachable,
                                             "definition_hash": definition_hash(definition)})
        return {"id": audience_id, "reachable": sized.reachable, "definition_hash": definition_hash(definition)}

    @app.get("/api/audiences")
    def audiences(request: Request) -> list[dict]:
        s = svc(request)
        rows = []
        for row in s.store.list_audiences():
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
            rows.append(row)
        return rows

    @app.get("/api/insights")
    def insights(request: Request) -> dict:
        s = svc(request)
        return build_insights(s.store, s.catalog)

    @app.get("/api/catalog")
    def catalog(request: Request) -> dict:
        c = svc(request).catalog
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
            "examples": EXAMPLES,
        }

    @app.get("/api/metrics")
    def metrics(request: Request) -> dict:
        return svc(request).metrics.snapshot()

    @app.get("/readyz")
    def readyz(request: Request) -> JSONResponse:
        s = svc(request)
        checks = {
            "profiles_loaded": s.engine.base > 0,
            "audit_store_writable": _safe(s.store.ping),
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
