"""System / introspection endpoints: version, node catalog, events, metrics."""

from __future__ import annotations

import os
import platform
import sys
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import require_manage_settings, require_view_audit
from app.core.startup import validate_settings
from app.infrastructure.config.settings import settings
from app.infrastructure.events.event_bus import event_bus
from app.infrastructure.observability.errors import error_aggregator
from app.infrastructure.scheduler.job_scheduler import job_scheduler
from app.services.security.principal import Principal
from app.services.security.sandbox import sandbox_status
from app.services.workflow.executors import executor_registry
from app.version import __version__

router = APIRouter(prefix="/system", tags=["System"])

_PROCESS_START = time.time()


@router.get("/info", summary="Runtime and build information")
def system_info() -> Dict[str, Any]:
    return {
        "name": settings.APP_NAME,
        "version": __version__,
        "environment": settings.ENVIRONMENT,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "uptime_seconds": round(time.time() - _PROCESS_START, 2),
        "features": {
            "shell_executor": settings.ALLOW_SHELL_EXECUTOR,
            "python_executor": settings.ALLOW_PYTHON_EXECUTOR,
            "javascript_executor": settings.ALLOW_JAVASCRIPT_EXECUTOR,
            "database_executor": settings.ALLOW_DATABASE_EXECUTOR,
            "docs": settings.ENABLE_DOCS,
            "rate_limit": settings.RATE_LIMIT_ENABLED,
            "auth": settings.AUTH_ENABLED,
            "metrics": settings.METRICS_ENABLED,
        },
        "script_sandbox": sandbox_status(),
    }


@router.get("/config/validation", summary="Startup configuration findings")
def config_validation(
    _: Principal = Depends(require_manage_settings),
) -> Dict[str, Any]:
    """Re-run the startup configuration checks against the live settings.

    Requires ``manage_settings``: the findings describe exactly how this
    deployment is misconfigured, which is not information to hand out freely.
    """
    findings = validate_settings(settings)
    return {
        "findings": [f.as_dict() for f in findings],
        "error_count": sum(1 for f in findings if f.severity == "error"),
        "warning_count": sum(1 for f in findings if f.severity == "warning"),
        "environment": settings.ENVIRONMENT,
    }


@router.get("/errors", summary="Aggregated recent errors")
def recent_errors(
    limit: int = Query(20, ge=1, le=100),
    since_seconds: Optional[float] = Query(
        None, ge=1, description="Only groups active within this window"
    ),
    _: Principal = Depends(require_view_audit),
) -> Dict[str, Any]:
    """Top error groups by frequency.

    In-process and bounded, so it is lost on restart and does not aggregate
    across replicas; see docs/SECURITY.md for the limitation.
    """
    return {
        "summary": error_aggregator.summary(),
        "groups": error_aggregator.top(limit=limit, since_seconds=since_seconds),
    }


@router.get("/metrics", summary="Lightweight process metrics")
def metrics() -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "uptime_seconds": round(time.time() - _PROCESS_START, 2),
        "pid": os.getpid(),
        "scheduler_running": job_scheduler.is_running,
        "scheduled_jobs": len(job_scheduler.list_jobs()),
    }
    try:  # resource is POSIX-only
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss is KB on Linux, bytes on macOS.
        divisor = 1024 if sys.platform == "darwin" else 1
        payload["max_rss_mb"] = round(usage.ru_maxrss / 1024 / divisor, 2)
        payload["user_cpu_seconds"] = round(usage.ru_utime, 3)
        payload["system_cpu_seconds"] = round(usage.ru_stime, 3)
    except Exception:  # pragma: no cover - platform dependent
        pass

    try:
        from app.services.workflow.engine import workflow_engine

        payload["active_executions"] = sum(
            1 for t in workflow_engine.active_tasks.values() if not t.done()
        )
    except Exception:  # pragma: no cover
        payload["active_executions"] = 0

    return payload


@router.get("/node-types", summary="Workflow node palette")
def node_types() -> List[Dict[str, Any]]:
    """Catalog of available node types, consumed by the workflow editor."""
    return executor_registry.catalog()


@router.get("/node-schemas", summary="Node input/output schemas")
def node_schemas(
    include_aliases: bool = Query(
        False, description="Include snake_case aliases of canonical node types"
    ),
    category: str = Query("", description="Optional category filter"),
) -> List[Dict[str, Any]]:
    """Full input and output schemas per node type (M4).

    The editor uses this to render typed property forms and to validate a node
    before the workflow is run. ``/node-types`` remains the lighter palette
    endpoint and is unchanged.
    """
    entries = executor_registry.schemas()
    if not include_aliases:
        entries = [e for e in entries if not e.get("is_alias")]
    if category:
        entries = [e for e in entries if e.get("category") == category]
    return entries


@router.get("/events", summary="Recent in-process events")
def recent_events(
    limit: int = Query(50, ge=1, le=200),
    event_type: str = Query("", description="Optional exact event name filter"),
) -> List[Dict[str, Any]]:
    return event_bus.recent(limit=limit, event_type=event_type or None)


@router.get("/scheduler/jobs", summary="Scheduled background jobs")
def scheduler_jobs() -> List[Dict[str, Any]]:
    return job_scheduler.list_jobs()
