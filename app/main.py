"""HTTP API and static UI.

    POST /api/interpret   plain-English request -> validated proposal (the only endpoint that calls the model)
    POST /api/check       re-validate after edits or clarification answers (deterministic)
    POST /api/size        audience size, consent exclusions, breakdowns, guardrail warnings
    POST /api/approve     server-side re-check + re-size, then an audit record with provenance
    GET  /api/audiences   approved audiences (audit trail)
    GET  /api/catalog     attributes and terms for the editor
    GET  /api/metrics     counters and latency percentiles
    GET  /healthz         liveness + readiness detail
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.audience import AudienceEngine
from app.catalog import Catalog, load_catalog
from app.interpret import Interpreter
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


def build_provider(settings: Settings) -> Provider:
    if settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY")
        provider: Provider = AnthropicProvider(settings.anthropic_api_key, settings.anthropic_model,
                                               settings.llm_timeout_s, settings.llm_max_retries)
        return RecordingProvider(provider, settings.fixtures_dir) if settings.record_fixtures else provider
    return ReplayProvider(settings.fixtures_dir)


def build_services(settings: Settings, provider: Provider | None = None) -> Services:
    catalog = load_catalog(settings.config_dir)
    if not settings.data_path.exists():
        log.info("generating_synthetic_data", extra={"rows": settings.profile_rows})
        generate(settings.data_path, settings.profile_rows)
    engine = AudienceEngine(settings.data_path, catalog, settings.min_audience, settings.broad_share)
    store = AuditStore(settings.audit_db_path)
    metrics = Metrics()
    interpreter = Interpreter(
        catalog=catalog,
        provider=provider or build_provider(settings),
        store=store,
        cache=TTLCache(settings.cache_ttl_s, settings.cache_max_entries),
        metrics=metrics,
        pricing=Pricing.load(settings.config_dir / "models.yaml"),
        max_request_chars=settings.max_request_chars,
    )
    log.info("startup", extra={"provider": interpreter.provider.name, "model": interpreter.provider.model,
                               "profiles": engine.base, "catalog_version": catalog.version})
    return Services(settings, catalog, engine, store, interpreter, metrics)


def create_app(settings: Settings | None = None, provider: Provider | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = build_services(settings, provider)
        yield

    app = FastAPI(title="Audience Assistant", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            elapsed = (time.perf_counter() - started) * 1000
            response.headers["x-request-id"] = rid
            if request.url.path.startswith("/api"):
                log.info("http_request", extra={"method": request.method, "path": request.url.path,
                                                "status_code": response.status_code, "duration_ms": round(elapsed, 1)})
                app.state.services.metrics.observe(f"http {request.method} {request.url.path}", elapsed)
            return response
        finally:
            request_id_var.reset(token)

    def svc(request: Request) -> Services:
        return request.app.state.services

    @app.post("/api/interpret", response_model=InterpretResult)
    def interpret(body: InterpretRequest, request: Request) -> InterpretResult:
        return svc(request).interpreter.interpret(body.request)

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
        checked = s.interpreter.check(body.definition, [], {})
        if checked.status != "ready":
            raise HTTPException(422, detail={"status": checked.status, "issues": [i.model_dump() for i in checked.issues]})
        unmapped = json.loads(interpretation["unmapped_json"])
        if unmapped and not body.acknowledged_unmapped:
            raise HTTPException(422, detail={"status": "needs_confirmation", "unmapped_phrases": unmapped})

        sized = s.engine.size(checked.definition)
        definition = checked.definition.model_dump()
        original = json.loads(interpretation["definition_json"])
        audience_id = s.store.record_approval({
            "name": body.name.strip(),
            "approver": body.approver.strip(),
            "request_id": body.request_id,
            "definition_json": json.dumps(definition),
            "definition_hash": definition_hash(definition),
            "edited_by_user": int(definition_hash(definition) != definition_hash(original)),
            "explanation": checked.explanation.summary,
            "channel": checked.definition.channel,
            "matched": sized.matched,
            "reachable": sized.reachable,
            "acknowledged_unmapped_json": json.dumps(unmapped if body.acknowledged_unmapped else []),
            "catalog_version": s.catalog.version,
            "glossary_version": s.catalog.glossary_version,
        })
        s.metrics.incr("audiences_approved")
        log.info("audience_approved", extra={"audience_id": audience_id, "reachable": sized.reachable,
                                             "definition_hash": definition_hash(definition)})
        return {"id": audience_id, "reachable": sized.reachable, "definition_hash": definition_hash(definition)}

    @app.get("/api/audiences")
    def audiences(request: Request) -> list[dict]:
        return svc(request).store.list_audiences()

    @app.get("/api/catalog")
    def catalog(request: Request) -> dict:
        c = svc(request).catalog
        return {
            "version": c.version,
            "attributes": [
                {"name": a.name, "label": a.label, "type": a.type, "values": list(a.values), "min": a.min,
                 "max": a.max, "unit": a.unit, "description": a.description}
                for a in c.attributes.values() if a.targetable
            ],
            "channels": list(c.channels),
            "examples": EXAMPLES,
        }

    @app.get("/api/metrics")
    def metrics(request: Request) -> dict:
        return svc(request).metrics.snapshot()

    @app.get("/healthz")
    def healthz(request: Request) -> dict:
        s = svc(request)
        return {"status": "ok", "profiles": s.engine.base, "provider": s.interpreter.provider.name,
                "model": s.interpreter.provider.model, "catalog_version": s.catalog.version}

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
    uvicorn.run(create_app(), host="127.0.0.1", port=8000, log_config=None, access_log=False)


if __name__ == "__main__":
    run()
