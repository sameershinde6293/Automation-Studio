"""Creator OS FastAPI application factory."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.core.errors import register_exception_handlers
from app.core.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger, setup_logging
from app.infrastructure.scheduler.job_scheduler import job_scheduler
from app.services.plugin_sdk.sdk import plugin_sdk
from app.version import __version__

logger = get_logger("app")

_STARTED_AT = time.time()


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
    if settings.RATE_LIMIT_ENABLED:
        application.add_middleware(
            RateLimitMiddleware,
            max_requests=settings.RATE_LIMIT_REQUESTS,
            window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
        )
    application.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.MAX_REQUEST_BYTES,
        exempt_paths=("/api/media/upload",),
    )
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(GZipMiddleware, minimum_size=1024)
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
        enterprise_router,
        execution_router,
        media_router,
        plugin_router,
        project_router,
        system_router,
        workflow_router,
    )

    application.include_router(project_router.router, prefix="/api")
    application.include_router(workflow_router.router, prefix="/api")
    application.include_router(execution_router.router, prefix="/api")
    application.include_router(ai_router.router, prefix="/api")
    application.include_router(media_router.router, prefix="/api")
    application.include_router(plugin_router.router, prefix="/api")
    application.include_router(enterprise_router.router, prefix="/api")
    application.include_router(system_router.router, prefix="/api")

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
    def readiness() -> Dict[str, Any]:
        from sqlalchemy import text

        from app.infrastructure.database.database import engine

        checks: Dict[str, str] = {}
        ready = True
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:  # pragma: no cover - depends on env
            checks["database"] = f"error: {type(exc).__name__}"
            ready = False

        checks["scheduler"] = "ok" if job_scheduler.is_running else "stopped"
        return {
            "status": "ready" if ready else "degraded",
            "version": settings.VERSION,
            "checks": checks,
        }

    return application


app = create_app()
