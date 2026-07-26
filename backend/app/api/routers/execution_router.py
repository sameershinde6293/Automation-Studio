"""Execution control, history and streaming API (M4).

Only endpoints that did **not** already exist are defined here. Workflow CRUD,
node/edge CRUD, graph save/validate, execution creation
(``POST /api/workflows/{id}/executions``), single-execution fetch and cancel all
remain in ``workflow_router`` and are not duplicated.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain.models.workflow import ExecutionStatus
from app.domain.repositories.workflow_repository import workflow_execution_repo
from app.infrastructure.config.settings import settings
from app.infrastructure.database.database import get_db
from app.services.workflow import streaming as stream_events
from app.services.workflow.engine import workflow_engine
from app.services.workflow.history import execution_history
from app.services.workflow.streaming import execution_broker, format_sse

router = APIRouter(prefix="/executions", tags=["Executions"])


# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #
class ReplayRequest(BaseModel):
    priority: Optional[int] = Field(
        None, description="0=critical, 10=high, 50=normal, 90=low"
    )
    input_data: Optional[Dict[str, Any]] = None
    start: bool = Field(True, description="Queue the new execution immediately")


class ResumeFailedRequest(BaseModel):
    priority: Optional[int] = None
    start: bool = True


def _require_execution(db: Session, execution_id: int):
    execution = workflow_execution_repo.get(db, execution_id)
    if not execution:
        raise NotFoundError(f"Execution {execution_id} not found.")
    return execution


def _status_payload(execution_id: int, action: str, changed: bool, message: str):
    return {
        "execution_id": execution_id,
        "action": action,
        "changed": changed,
        "message": message,
    }


# --------------------------------------------------------------------------- #
# Queue and stats (static paths first so they never shadow /{execution_id})
# --------------------------------------------------------------------------- #
@router.get("/queue", summary="Execution queue and worker status")
def queue_status() -> Dict[str, Any]:
    return workflow_engine.queue_status()


@router.get("/stats", summary="Aggregate execution statistics")
def execution_stats(
    workflow_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    return execution_history.stats(db, workflow_id)


@router.get("", summary="Search execution history")
@router.get("/", include_in_schema=False)
def list_executions(
    workflow_id: Optional[int] = Query(None),
    status: List[str] = Query(default_factory=list, description="Repeatable status filter"),
    trigger: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Matches workflow name or error text"),
    created_after: Optional[str] = Query(None, description="ISO timestamp"),
    created_before: Optional[str] = Query(None, description="ISO timestamp"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Global, filterable execution history.

    ``GET /api/workflows/{id}/executions`` still exists for the simple
    per-workflow list; this endpoint adds cross-workflow search and filtering.
    """
    for value in status:
        if value.strip().upper() not in ExecutionStatus.__members__:
            raise ValidationError(
                f"Unknown status {value!r}.",
                details={"valid": sorted(ExecutionStatus.__members__)},
            )
    return execution_history.search(
        db,
        workflow_id=workflow_id,
        statuses=status or None,
        trigger=trigger,
        search=search,
        created_after=created_after,
        created_before=created_before,
        skip=skip,
        limit=limit,
    )


# --------------------------------------------------------------------------- #
# Control
# --------------------------------------------------------------------------- #
@router.post("/{execution_id}/pause", summary="Pause a running execution")
def pause_execution(execution_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    execution = _require_execution(db, execution_id)
    if execution.status and execution.status.is_terminal:
        raise ConflictError(
            f"Execution {execution_id} already finished ({execution.status.value}).",
        )
    changed = workflow_engine.pause(execution_id)
    return _status_payload(
        execution_id,
        "pause",
        changed,
        "Pause requested; in-flight nodes will finish."
        if changed
        else "Execution is not running or is already paused.",
    )


@router.post("/{execution_id}/resume", summary="Resume a paused execution")
def resume_execution(execution_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    execution = _require_execution(db, execution_id)
    if execution.status and execution.status.is_terminal:
        raise ConflictError(
            f"Execution {execution_id} already finished ({execution.status.value}).",
        )
    changed = workflow_engine.resume(execution_id)
    return _status_payload(
        execution_id,
        "resume",
        changed,
        "Execution resumed." if changed else "Execution is not paused.",
    )


@router.post("/{execution_id}/stop", summary="Gracefully stop an execution")
def stop_execution(execution_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    execution = _require_execution(db, execution_id)
    if execution.status and execution.status.is_terminal:
        raise ConflictError(
            f"Execution {execution_id} already finished ({execution.status.value}).",
        )
    changed = workflow_engine.stop(execution_id)
    return _status_payload(
        execution_id,
        "stop",
        changed,
        "Graceful stop requested; in-flight nodes will finish."
        if changed
        else "Execution is not running.",
    )


# --------------------------------------------------------------------------- #
# Replay / resume
# --------------------------------------------------------------------------- #
@router.post("/{execution_id}/replay", status_code=201, summary="Replay an execution")
def replay_execution(
    execution_id: int,
    payload: ReplayRequest | None = None,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    payload = payload or ReplayRequest()
    new_execution = execution_history.replay(
        db,
        execution_id,
        priority=payload.priority,
        input_data=payload.input_data,
    )
    queued = None
    if payload.start:
        queued = workflow_engine.enqueue(
            new_execution.id,
            priority=new_execution.priority,
            workflow_id=new_execution.workflow_id,
        )
    return {
        "execution_id": new_execution.id,
        "workflow_id": new_execution.workflow_id,
        "parent_execution_id": execution_id,
        "replay_of": "replay",
        "status": (
            ExecutionStatus.QUEUED.value if payload.start else ExecutionStatus.PENDING.value
        ),
        "queue": queued,
    }


@router.post(
    "/{execution_id}/resume-failed",
    status_code=201,
    summary="Retry a failed execution with prior context",
)
def resume_failed_execution(
    execution_id: int,
    payload: ResumeFailedRequest | None = None,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    payload = payload or ResumeFailedRequest()
    new_execution = execution_history.resume_failed(
        db, execution_id, priority=payload.priority
    )
    queued = None
    if payload.start:
        queued = workflow_engine.enqueue(
            new_execution.id,
            priority=new_execution.priority,
            workflow_id=new_execution.workflow_id,
        )
    return {
        "execution_id": new_execution.id,
        "workflow_id": new_execution.workflow_id,
        "parent_execution_id": execution_id,
        "replay_of": "resume_failed",
        "status": (
            ExecutionStatus.QUEUED.value if payload.start else ExecutionStatus.PENDING.value
        ),
        "queue": queued,
        "note": (
            "Completed node outputs are seeded into the new run's context; the "
            "graph is still traversed from the start."
        ),
    }


# --------------------------------------------------------------------------- #
# History detail
# --------------------------------------------------------------------------- #
@router.get("/{execution_id}/logs", summary="Execution log records")
def execution_logs(
    execution_id: int,
    after_sequence: int = Query(0, ge=0, description="Return logs newer than this"),
    level: Optional[str] = Query(None, description="DEBUG|INFO|WARNING|ERROR"),
    node_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    return execution_history.get_logs(
        db,
        execution_id,
        after_sequence=after_sequence,
        level=level,
        node_id=node_id,
        search=search,
        limit=limit,
    )


@router.get("/{execution_id}/timeline", summary="Node-by-node execution timeline")
def execution_timeline(execution_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return execution_history.get_timeline(db, execution_id)


@router.get("/{execution_id}/lineage", summary="Replay/resume lineage")
def execution_lineage(execution_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return execution_history.lineage(db, execution_id)


@router.get("/{execution_id}", summary="Execution detail with node results")
def execution_detail(execution_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    payload = execution_history.get_detail(db, execution_id)
    payload["is_running"] = workflow_engine.is_running(execution_id)
    payload["is_paused"] = workflow_engine.is_paused(execution_id)
    return payload


# --------------------------------------------------------------------------- #
# Live streaming (SSE)
# --------------------------------------------------------------------------- #
async def _event_stream(
    request: Request,
    execution_id: int,
    after_sequence: int,
    already_finished: bool = False,
    final_status: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Yield SSE frames for one execution until it finishes or the client leaves.

    Terminating correctly matters in three distinct cases:

    1. The run finishes while we are streaming -> the live ``execution.finished``
       event breaks the loop.
    2. The run already finished but its events are still buffered -> the
       backfill contains ``execution.finished``, so we must stop right after
       replaying it. (Missing this check made the endpoint heartbeat forever.)
    3. The run finished long ago and its buffer was evicted -> there is nothing
       to replay, so we synthesise a terminal frame from the persisted status.
    """
    subscription = execution_broker.subscribe(execution_id)
    heartbeat = max(1.0, settings.EXECUTION_STREAM_HEARTBEAT_SECONDS)
    try:
        # Backfill anything the client missed before subscribing.
        replayed_terminal = False
        for event in execution_broker.replay_events(execution_id, after_sequence):
            yield format_sse(event)
            if event.event == stream_events.EVENT_EXECUTION_FINISHED:
                replayed_terminal = True

        if replayed_terminal:
            return

        if already_finished:
            # Case 3: nothing buffered, but the run is over. Tell the client so
            # it can close instead of waiting for an event that never comes.
            yield format_sse(
                stream_events.ExecutionEvent(
                    execution_id=execution_id,
                    event=stream_events.EVENT_EXECUTION_FINISHED,
                    sequence=execution_broker.next_sequence(execution_id),
                    payload={"status": final_status, "replayed": True},
                )
            )
            return

        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(
                    subscription.queue.get(), timeout=heartbeat
                )
            except asyncio.TimeoutError:
                # Comment frame keeps proxies from closing an idle connection.
                yield ": keepalive\n\n"
                continue
            yield format_sse(event)
            if event.event == stream_events.EVENT_EXECUTION_FINISHED:
                break
    except asyncio.CancelledError:  # pragma: no cover - client disconnect
        raise
    finally:
        execution_broker.unsubscribe(subscription)
        execution_broker.cleanup(execution_id)


@router.get("/{execution_id}/stream", summary="Live execution event stream (SSE)")
async def execution_stream(
    execution_id: int,
    request: Request,
    after_sequence: int = Query(0, ge=0, description="Resume after this event id"),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Server-Sent Events stream of node/progress/log events for one execution.

    Replaces the M3 placeholder that simulated progress in the UI. Subscriber
    queues are bounded, so a slow client is dropped rather than stalling the
    engine.
    """
    execution = _require_execution(db, execution_id)
    # Snapshot terminality here, while the request-scoped session is alive; the
    # generator runs after the response starts and must not touch the ORM.
    already_finished = bool(execution.status and execution.status.is_terminal)
    final_status = execution.status.value if execution.status else None
    return StreamingResponse(
        _event_stream(
            request, execution_id, after_sequence, already_finished, final_status
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{execution_id}/events", summary="Buffered live events (polling fallback)")
def execution_events(
    execution_id: int,
    after_sequence: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Polling alternative to SSE for clients that cannot hold a connection."""
    _require_execution(db, execution_id)
    events = execution_broker.replay_events(execution_id, after_sequence)
    return {
        "execution_id": execution_id,
        "events": [event.to_dict() for event in events],
        "last_sequence": events[-1].sequence if events else after_sequence,
    }
