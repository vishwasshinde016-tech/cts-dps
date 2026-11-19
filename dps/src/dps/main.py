"""Application factory. Run: ``uvicorn --factory dps.main:create_app --port 9400``."""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request

from dps import __version__, errors
from dps.advisory import ask
from dps.advisory.advisor import Advisor
from dps.api.access import CoreCpAccessChecker, CpAccessChecker
from dps.api.auth import Authenticator
from dps.api.documents import router
from dps.api.evidence import router as evidence_router
from dps.api.jobs import router as job_router
from dps.api.ratelimit import RateLimiter
from dps.api.review import router as review_router
from dps.db import Base, Database, require_snapshot_isolation
from dps.ingest.file_store import FileStore
from dps.ops import observability as obs
from dps.settings import Settings, get_settings, llm_allowed

CORRELATION_HEADER = "X-Correlation-Id"


def create_app(settings: Settings | None = None, *, cp_checker: CpAccessChecker | None = None,
               create_schema: bool = False, advisor: Advisor | None = None,
               core_fetch: ask.Fetch | None = None) -> FastAPI:
    settings = settings or get_settings()
    obs.setup("dps-api")
    app = FastAPI(title="Document Processing Service", version=__version__)
    app.state.settings = settings
    app.state.db = Database(settings.database_url)
    require_snapshot_isolation(app.state.db.engine)
    app.state.store = FileStore(settings.storage_dir)
    app.state.limiter = RateLimiter()
    app.state.authenticator = Authenticator(settings)

    @app.middleware("http")
    async def _tenant_scope(request, call_next):  # G2: every read below is scoped to this deployment's tenant
        from dps.api.access import reset_tenant, set_tenant

        token = set_tenant(settings.tenant_id)
        try:
            return await call_next(request)
        finally:
            reset_tenant(token)
    app.state.cp_checker = cp_checker or CoreCpAccessChecker(settings.core_base_url, settings.core_timeout_seconds)
    # Phase 3a: every model call is masked, cached and kept within the monthly budget (ai_guard).
    # Phase 3: through the model gateway (routes, retries, breaker, budgets) like every other model call.
    completion = None
    if llm_allowed(settings) and not advisor:  # pragma: no cover - only if the bank ever approves an LLM
        from dps.agent.gateway import from_settings

        completion = from_settings(settings, app.state.db.session_factory).completion("strong")
    app.state.advisor = advisor or Advisor(completion)
    app.state.core_fetch = core_fetch or ask.core_fetch(settings.core_base_url, settings.core_timeout_seconds)
    if create_schema:  # tests / local only; production uses Alembic migrations
        Base.metadata.create_all(app.state.db.engine)

    @app.middleware("http")
    async def correlation_id(request: Request, call_next):
        cid = request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())
        request.state.correlation_id = cid
        timer = obs.Timer()
        # Every line written while serving the request carries its id; the trace continues Core's or the gateway's.
        with obs.bound(request_id=cid), obs.span(f"{request.method} {request.url.path}", headers=dict(request.headers),
                                                 **{"http.request.method": request.method}):
            response = await call_next(request)
            if request.url.path != "/health":
                ms = round(timer.seconds * 1000)
                with obs.bound(user=getattr(request.state, "user", None)):
                    obs.event("REQUEST", f"{request.method} {request.url.path} {response.status_code} {ms} ms",
                              method=request.method, path=request.url.path, status=response.status_code, ms=ms)
                obs.count("dps.http.requests", 1, method=request.method, status=response.status_code)
                obs.observe("dps.http.duration", timer.seconds, method=request.method)
        response.headers[CORRELATION_HEADER] = cid
        return response

    errors.install(app)
    if settings.legacy_api:  # G12: the old Covenexa routes; off in CTS (the DMS uploads, Spring approves)
        app.include_router(router)
    else:
        app.add_api_route("/health", lambda: {"status": "UP"}, methods=["GET"])
    app.include_router(job_router)  # CTS job API (contracts/dps-jobs-api.yaml)
    app.include_router(evidence_router)  # page images, runs, ask, usage, /api/data/values
    app.include_router(review_router)  # review, reclassify, quality, /api/v1/ai/covenant-status
    logging.getLogger("dps").info("DPS %s started (auth=%s)", __version__, settings.auth_mode.value)
    return app

