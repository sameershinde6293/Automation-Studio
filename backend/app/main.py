"""Creator OS FastAPI application factory."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.errors import register_exception_handlers
from app.core.middleware import (
    BodySizeLimitMiddleware,
    CSRFMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.startup import enforce_startup_validation
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger, setup_logging
from app.infrastructure.observability.metrics import app_info, registry
from app.infrastructure.scheduler.job_scheduler import job_scheduler
from app.services.plugin_sdk.sdk import plugin_sdk
from app.version import __version__

logger = get_logger("app")

_STARTED_AT = time.time()

#: Findings from startup configuration validation, surfaced on /health/ready.
_STARTUP_FINDINGS: list = []

#: Current API version prefix. Routes are served both unprefixed (``/api/...``,
#: permanently supported) and version-pinned (``/api/v1/...``).
API_VERSION = "v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(
        level=settings.LOG_LEVEL,
        fmt=settings.LOG_FORMAT,
        log_file=settings.LOG_FILE or None,
        force=True,
    )
    logger.info(
        "Creator OS backend %s starting (env=%s)", __version__, settings.ENVIRONMENT
    )
    startup_began = time.perf_counter()

    # M5: refuse to start an unsafe production configuration. Raises
    # StartupValidationError in production; only warns elsewhere.
    global _STARTUP_FINDINGS
    _STARTUP_FINDINGS = enforce_startup_validation(settings)
    app.state.startup_findings = [f.as_dict() for f in _STARTUP_FINDINGS]

    app_info.set(1, version=__version__, environment=settings.ENVIRONMENT)

    # M5: create the initial administrator on first run when configured, so a
    # fresh authenticated deployment is reachable without manual DB surgery.
    if settings.AUTH_ENABLED and settings.AUTH_BOOTSTRAP_USERNAME:
        try:
            from app.infrastructure.database.database import SessionLocal
            from app.services.security.auth_service import auth_service

            with SessionLocal() as db:
                auth_service.bootstrap_admin(db)
        except Exception:
            logger.exception("Initial administrator bootstrap failed.")

    try:
        job_scheduler.start()
    except Exception:
        logger.exception("Job scheduler failed to start; continuing without it.")

    # M4: start the execution worker pool on the serving loop. Workers are
    # long-lived tasks, so they must be owned by the app lifespan rather than
    # created lazily inside a request.
    try:
        from app.services.workflow.engine import workflow_engine

        if workflow_engine.start_workers():
            logger.info(
                "Execution worker pool started (%s workers).",
                settings.EXECUTION_MAX_WORKERS,
            )
    except Exception:
        logger.exception("Execution worker pool failed to start.")

    plugin_sdk.trigger_hook(plugin_sdk.HOOK_APP_STARTUP)

    app.state.started_at = time.time()
    app.state.ready = True
    logger.info(
        "Startup complete in %.1fms", (time.perf_counter() - startup_began) * 1000
    )

    try:
        yield
    finally:
        app.state.ready = False
        logger.info("Creator OS backend shutting down...")
        plugin_sdk.trigger_hook(plugin_sdk.HOOK_APP_SHUTDOWN)

        try:
            from app.services.workflow.engine import workflow_engine

            await workflow_engine.shutdown()
        except Exception:
            logger.exception("Error while shutting down the workflow engine.")

        try:
            from app.services.media.pipeline import media_pipeline

            await media_pipeline.shutdown()
        except Exception:
            logger.exception("Error while shutting down the media pipeline.")

        try:
            job_scheduler.shutdown()
        except Exception:
            logger.exception("Error while shutting down the job scheduler.")

        try:
            from app.infrastructure.database.database import dispose_engine

            dispose_engine()
        except Exception:
            logger.exception("Error while disposing the database engine.")

        logger.info("Shutdown complete.")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    application = FastAPI(
        title=settings.APP_NAME,
        version=settings.VERSION,
        lifespan=lifespan,
        docs_url="/docs" if settings.ENABLE_DOCS else None,
        redoc_url="/redoc" if settings.ENABLE_DOCS else None,
        openapi_url="/openapi.json" if settings.ENABLE_DOCS else None,
        description=(
            "Local-first automation, AI and media orchestration API for Creator OS."
        ),
    )

    # Middleware is applied bottom-up; RequestContext must wrap everything so
    # each log line and error response carries the request id.
    if settings.CSRF_PROTECTION_ENABLED:
        application.add_middleware(
            CSRFMiddleware,
            header_name=settings.CSRF_HEADER_NAME,
            cookie_name=settings.CSRF_COOKIE_NAME,
            api_key_header=settings.AUTH_API_KEY_HEADER,
        )
    if settings.RATE_LIMIT_ENABLED:
        application.add_middleware(
            RateLimitMiddleware,
            max_requests=settings.RATE_LIMIT_REQUESTS,
            window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
            trust_proxy=settings.TRUST_PROXY_HEADERS,
            auth_max_requests=settings.AUTH_RATE_LIMIT_REQUESTS,
            auth_window_seconds=settings.AUTH_RATE_LIMIT_WINDOW_SECONDS,
        )
    application.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.MAX_REQUEST_BYTES,
        exempt_paths=("/api/media/upload",),
    )
    application.add_middleware(
        SecurityHeadersMiddleware,
        hsts=settings.SECURITY_HSTS_ENABLED,
        hsts_max_age=settings.SECURITY_HSTS_MAX_AGE,
    )
    application.add_middleware(GZipMiddleware, minimum_size=1024)
    # Host-header validation must run before routing so a rebinding attempt is
    # rejected outright. ["*"] (the local-desktop default) disables the check.
    if settings.ALLOWED_HOSTS and settings.ALLOWED_HOSTS != ["*"]:
        application.add_middleware(
            TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS
        )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time-ms"],
    )
    application.add_middleware(RequestContextMiddleware)

    register_exception_handlers(application)

    # Routers are imported lazily so that importing app.main stays cheap.
    from app.api.routers import (
        ai_router,
        auth_router,
        enterprise_router,
        execution_router,
        media_router,
        plugin_router,
        project_router,
        system_router,
        workflow_router,
    )

    routers = (
        auth_router.router,
        project_router.router,
        workflow_router.router,
        execution_router.router,
        ai_router.router,
        media_router.router,
        plugin_router.router,
        enterprise_router.router,
        system_router.router,
    )

    for router in routers:
        application.include_router(router, prefix="/api")

    # M5: explicit API versioning. Every route is additionally served under
    # /api/v1 so clients can pin a version, while the unprefixed /api paths
    # remain permanently supported for backward compatibility.
    for router in routers:
        application.include_router(
            router, prefix=f"/api/{API_VERSION}", include_in_schema=False
        )

    @application.get("/", tags=["System"], summary="Service root")
    def read_root() -> Dict[str, Any]:
        return {"status": "ok", "message": f"{settings.APP_NAME} is running"}

    @application.get("/health", tags=["System"], summary="Liveness probe")
    def health_check() -> Dict[str, Any]:
        return {"status": "healthy"}

    @application.get("/health/live", tags=["System"], summary="Liveness probe")
    def liveness() -> Dict[str, Any]:
        return {"status": "healthy", "uptime_seconds": round(time.time() - _STARTED_AT, 2)}

    @application.get("/health/ready", tags=["System"], summary="Readiness probe")
    def readiness(response: Response) -> Dict[str, Any]:
        """Report whether this instance can serve traffic.

        Returns 503 when a hard dependency is down, so an orchestrator removes
        the instance from rotation instead of sending it requests it cannot
        satisfy.
        """
        from sqlalchemy import text

        from app.infrastructure.database.database import engine

        checks: Dict[str, Any] = {}
        ready = True
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:  # pragma: no cover - depends on env
            checks["database"] = f"error: {type(exc).__name__}"
            ready = False

        checks["scheduler"] = "ok" if job_scheduler.is_running else "stopped"

        # The worker pool is what actually runs queued workflows; an instance
        # without it is not ready to accept execution work.
        try:
            from app.services.workflow.engine import workflow_engine

            queue_state = workflow_engine.queue_status()
            workers_up = bool(queue_state.get("workers", {}).get("running"))
            checks["execution_workers"] = "ok" if workers_up else "stopped"
            checks["queue_depth"] = queue_state.get("queue_size", 0)
        except Exception as exc:  # pragma: no cover - defensive
            checks["execution_workers"] = f"error: {type(exc).__name__}"

        findings = getattr(application.state, "startup_findings", [])
        blocking = [f for f in findings if f.get("severity") == "error"]
        if blocking:
            checks["configuration"] = f"{len(blocking)} unresolved error(s)"
            ready = False
        elif findings:
            checks["configuration"] = f"{len(findings)} warning(s)"
        else:
            checks["configuration"] = "ok"

        if not ready:
            response.status_code = 503
        return {
            "status": "ready" if ready else "degraded",
            "version": settings.VERSION,
            "checks": checks,
        }

    if settings.METRICS_ENABLED:

        @application.get(
            "/metrics",
            tags=["System"],
            summary="Prometheus metrics",
            response_class=PlainTextResponse,
        )
        def prometheus_metrics(request: Request) -> PlainTextResponse:
            """Prometheus text exposition of application metrics.

            Left unauthenticated by default so a scraper on a private network
            works out of the box; set ``METRICS_REQUIRE_AUTH=true`` to require
            the ``manage_settings`` permission.
            """
            if settings.METRICS_REQUIRE_AUTH:
                from app.api.dependencies import PERM_MANAGE_SETTINGS
                from app.core.errors import ForbiddenError
                from app.infrastructure.database.database import SessionLocal
                from app.api.dependencies import get_principal

                with SessionLocal() as db:
                    principal = get_principal(request, db)
                if not principal.has_permission(PERM_MANAGE_SETTINGS):
                    raise ForbiddenError(
                        "The metrics endpoint requires the manage_settings permission."
                    )

            _refresh_runtime_gauges()
            return PlainTextResponse(
                registry.render(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

    return application


def _refresh_runtime_gauges() -> None:
    """Sample point-in-time engine state just before metrics are rendered.

    Queue depth and active executions are gauges, not events, so they are read
    on scrape rather than pushed on every state change.
    """
    try:
        from app.infrastructure.observability.metrics import (
            execution_queue_depth,
            executions_active,
        )
        from app.services.workflow.engine import workflow_engine

        state = workflow_engine.queue_status()
        execution_queue_depth.set(state.get("queue_size", 0))
        executions_active.set(len(state.get("running_executions", [])))
    except Exception:  # pragma: no cover - metrics must never break a scrape
        logger.debug("Could not refresh runtime gauges", exc_info=True)

    # M9-F1: expose database pool saturation. Pool exhaustion presents as
    # requests hanging on checkout for DB_POOL_TIMEOUT_SECONDS, which looks
    # identical to a slow database from the outside; these gauges tell the two
    # apart. SQLite uses a non-queue pool and simply reports nothing.
    try:
        from app.infrastructure.database.database import engine
        from app.infrastructure.observability.metrics import (
            db_pool_available,
            db_pool_capacity,
            db_pool_checked_out,
            db_pool_overflow,
            db_pool_size,
            db_pool_utilisation_ratio,
        )

        pool = engine.pool
        if hasattr(pool, "checkedout"):
            checked_out = pool.checkedout()
            db_pool_checked_out.set(checked_out)
            if hasattr(pool, "size"):
                db_pool_size.set(pool.size())
            if hasattr(pool, "checkedin"):
                db_pool_available.set(pool.checkedin())
            if hasattr(pool, "overflow"):
                db_pool_overflow.set(pool.overflow())
            capacity = int(settings.DB_POOL_SIZE) + int(settings.DB_MAX_OVERFLOW)
            if capacity > 0:
                db_pool_capacity.set(capacity)
                db_pool_utilisation_ratio.set(round(checked_out / capacity, 4))
    except Exception:  # pragma: no cover - metrics must never break a scrape
        logger.debug("Could not refresh database pool gauges", exc_info=True)


app = create_app()
