"""Execution history: search, replay, resume-failed, timelines and stats.

M1/M2/M3 persisted executions but exposed only "list by workflow". There was no
global history, no filtering, and the checkpoint written into
``WorkflowExecution.state`` was never read back, so a failed run could not be
resumed (gaps R8/R9/B7).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain.models.workflow import (
    ExecutionPriority,
    ExecutionStatus,
    NodeExecution,
)
from app.domain.repositories.workflow_repository import (
    WorkflowExecutionCreate,
    edge_repo,
    execution_log_repo,
    node_execution_repo,
    node_repo,
    workflow_execution_repo,
    workflow_repo,
)
from app.infrastructure.logging.logger import get_logger

logger = get_logger("workflow.history")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialize_execution(execution, *, include_state: bool = False) -> Dict[str, Any]:
    """Compact representation used by list endpoints."""
    payload: Dict[str, Any] = {
        "id": execution.id,
        "workflow_id": execution.workflow_id,
        "status": execution.status.value if execution.status else None,
        "trigger": execution.trigger,
        "priority": execution.priority,
        "error": execution.error,
        "created_at": str(execution.created_at) if execution.created_at else None,
        "queued_at": execution.queued_at,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
        "parent_execution_id": execution.parent_execution_id,
        "replay_of": execution.replay_of,
        "metrics": execution.metrics or {},
    }
    if include_state:
        payload["state"] = execution.state or {}
        payload["input_data"] = execution.input_data or {}
    return payload


def serialize_node_execution(node_exec) -> Dict[str, Any]:
    return {
        "id": node_exec.id,
        "node_id": node_exec.node_id,
        "status": node_exec.status.value if node_exec.status else None,
        "output_data": node_exec.output_data,
        "error": node_exec.error,
        "error_code": node_exec.error_code,
        "retry_count": node_exec.retry_count,
        "duration_ms": node_exec.duration_ms,
        "queued_ms": node_exec.queued_ms,
        "started_at": node_exec.started_at,
        "finished_at": node_exec.finished_at,
        "iteration": node_exec.iteration,
        "attempt_metrics": node_exec.attempt_metrics,
    }


def serialize_log(row) -> Dict[str, Any]:
    return {
        "id": row.id,
        "sequence": row.sequence,
        "level": row.level.value if hasattr(row.level, "value") else str(row.level),
        "message": row.message,
        "node_id": row.node_id,
        "context": row.context,
        "at": str(row.created_at) if row.created_at else None,
    }


class ExecutionHistoryService:
    """Query and re-run past executions."""

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #
    def search(
        self,
        db: Session,
        *,
        workflow_id: Optional[int] = None,
        statuses: Optional[Sequence[str]] = None,
        trigger: Optional[str] = None,
        search: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> Dict[str, Any]:
        rows = workflow_execution_repo.search(
            db,
            workflow_id=workflow_id,
            statuses=statuses,
            trigger=trigger,
            search=search,
            created_after=created_after,
            created_before=created_before,
            skip=skip,
            limit=limit,
        )
        total = workflow_execution_repo.count_filtered(
            db,
            workflow_id=workflow_id,
            statuses=statuses,
            trigger=trigger,
            search=search,
            created_after=created_after,
            created_before=created_before,
        )
        # Resolve workflow names in one query rather than N.
        workflow_ids = {row.workflow_id for row in rows}
        names: Dict[int, str] = {}
        if workflow_ids:
            for workflow in (
                db.query(workflow_repo.model)
                .filter(workflow_repo.model.id.in_(workflow_ids))
                .all()
            ):
                names[workflow.id] = workflow.name

        items = []
        for row in rows:
            item = serialize_execution(row)
            item["workflow_name"] = names.get(row.workflow_id)
            items.append(item)

        return {
            "items": items,
            "total": total,
            "skip": skip,
            "limit": limit,
            "has_more": skip + len(items) < total,
        }

    def get_detail(self, db: Session, execution_id: int) -> Dict[str, Any]:
        execution = workflow_execution_repo.get(db, execution_id)
        if not execution:
            raise NotFoundError(f"Execution {execution_id} not found.")
        node_execs = node_execution_repo.get_by_execution(db, execution_id)
        workflow = workflow_repo.get(db, execution.workflow_id)

        payload = serialize_execution(execution, include_state=True)
        payload["workflow_name"] = workflow.name if workflow else None
        payload["node_executions"] = [serialize_node_execution(n) for n in node_execs]
        payload["log_count"] = execution_log_repo.count_by_execution(db, execution_id)
        return payload

    def get_logs(
        self,
        db: Session,
        execution_id: int,
        *,
        after_sequence: int = 0,
        level: Optional[str] = None,
        node_id: Optional[int] = None,
        search: Optional[str] = None,
        limit: int = 500,
    ) -> Dict[str, Any]:
        if not workflow_execution_repo.exists(db, execution_id):
            raise NotFoundError(f"Execution {execution_id} not found.")
        rows = execution_log_repo.get_by_execution(
            db,
            execution_id,
            after_sequence=after_sequence,
            level=level,
            node_id=node_id,
            search=search,
            limit=limit,
        )
        items = [serialize_log(row) for row in rows]
        return {
            "execution_id": execution_id,
            "items": items,
            "count": len(items),
            "last_sequence": items[-1]["sequence"] if items else after_sequence,
        }

    def get_timeline(self, db: Session, execution_id: int) -> Dict[str, Any]:
        """Node-by-node timing data for a Gantt-style view."""
        execution = workflow_execution_repo.get(db, execution_id)
        if not execution:
            raise NotFoundError(f"Execution {execution_id} not found.")
        node_execs = node_execution_repo.get_by_execution(db, execution_id)
        nodes = {n.id: n for n in node_repo.get_by_workflow(db, execution.workflow_id)}

        entries = []
        for item in node_execs:
            node = nodes.get(item.node_id)
            entries.append(
                {
                    "node_id": item.node_id,
                    "node_name": node.name if node else None,
                    "node_type": node.node_type if node else None,
                    "status": item.status.value if item.status else None,
                    "started_at": item.started_at,
                    "finished_at": item.finished_at,
                    "duration_ms": item.duration_ms or 0.0,
                    "queued_ms": item.queued_ms or 0.0,
                    "retry_count": item.retry_count,
                    "iteration": item.iteration,
                    "error_code": item.error_code,
                }
            )
        entries.sort(key=lambda e: (e["started_at"] or "", e["node_id"]))

        total = sum(e["duration_ms"] for e in entries)
        slowest = max(entries, key=lambda e: e["duration_ms"], default=None)
        return {
            "execution_id": execution_id,
            "status": execution.status.value if execution.status else None,
            "started_at": execution.started_at,
            "finished_at": execution.finished_at,
            "entries": entries,
            "node_count": len(entries),
            "total_node_duration_ms": round(total, 3),
            "slowest_node": slowest,
            "metrics": execution.metrics or {},
        }

    def stats(self, db: Session, workflow_id: Optional[int] = None) -> Dict[str, Any]:
        counts = workflow_execution_repo.status_counts(db, workflow_id)
        total = sum(counts.values())
        completed = counts.get(ExecutionStatus.COMPLETED.value, 0)
        failed = counts.get(ExecutionStatus.FAILED.value, 0)
        finished = completed + failed

        recent = workflow_execution_repo.search(
            db, workflow_id=workflow_id, statuses=None, skip=0, limit=100
        )
        durations = [
            row.metrics.get("duration_ms")
            for row in recent
            if isinstance(row.metrics, dict) and row.metrics.get("duration_ms")
        ]
        tokens = sum(
            int(row.metrics.get("total_tokens", 0) or 0)
            for row in recent
            if isinstance(row.metrics, dict)
        )
        cost = sum(
            float(row.metrics.get("cost_usd", 0.0) or 0.0)
            for row in recent
            if isinstance(row.metrics, dict)
        )
        return {
            "workflow_id": workflow_id,
            "total": total,
            "by_status": counts,
            "success_rate": round(completed / finished, 4) if finished else None,
            "avg_duration_ms": (
                round(sum(durations) / len(durations), 2) if durations else None
            ),
            "sampled_runs": len(recent),
            "total_tokens": tokens,
            "total_cost_usd": round(cost, 6),
        }

    # ------------------------------------------------------------------ #
    # Replay / resume
    # ------------------------------------------------------------------ #
    def replay(
        self,
        db: Session,
        execution_id: int,
        *,
        priority: Optional[int] = None,
        input_data: Optional[Dict[str, Any]] = None,
    ):
        """Create a fresh execution of the same workflow.

        The new run starts from scratch; it inherits the original's inputs
        unless new ones are supplied, and records lineage via
        ``parent_execution_id``.
        """
        source = workflow_execution_repo.get(db, execution_id)
        if not source:
            raise NotFoundError(f"Execution {execution_id} not found.")
        if not workflow_repo.exists(db, source.workflow_id):
            raise NotFoundError(
                f"Workflow {source.workflow_id} no longer exists; cannot replay."
            )

        return workflow_execution_repo.create(
            db,
            WorkflowExecutionCreate(
                workflow_id=source.workflow_id,
                trigger=f"replay:{execution_id}",
                priority=(
                    source.priority if priority is None else int(priority)
                ),
                parent_execution_id=execution_id,
                replay_of="replay",
                input_data=(
                    dict(input_data)
                    if input_data is not None
                    else dict(source.input_data or {})
                ),
            ),
        )

    def resume_failed(
        self,
        db: Session,
        execution_id: int,
        *,
        priority: Optional[int] = None,
    ):
        """Create a run that reuses successful node outputs and retries the rest.

        The successful nodes' outputs from the source run are seeded into the
        new run's ``input_data`` under ``__resume__``, and the previously failed
        node ids are recorded so the engine's context starts pre-populated.

        Honest limitation: the engine still walks the whole graph; already-
        completed nodes re-execute unless they are pure. This is a *retry with
        prior context*, not true mid-graph resumption. See EXECUTION_ENGINE.md.
        """
        source = workflow_execution_repo.get(db, execution_id)
        if not source:
            raise NotFoundError(f"Execution {execution_id} not found.")
        if source.status not in {
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            raise ConflictError(
                "Only FAILED or CANCELLED executions can be resumed.",
                details={
                    "status": source.status.value if source.status else None
                },
            )

        node_execs = node_execution_repo.get_by_execution(db, execution_id)
        completed_outputs: Dict[str, Any] = {}
        failed_nodes: List[int] = []
        for item in node_execs:
            if item.status == ExecutionStatus.COMPLETED:
                completed_outputs[str(item.node_id)] = item.output_data
            elif item.status in {ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}:
                failed_nodes.append(item.node_id)

        payload = dict(source.input_data or {})
        payload["__resume__"] = {
            "source_execution_id": execution_id,
            "completed_outputs": completed_outputs,
            "failed_nodes": failed_nodes,
            "resumed_at": _now_iso(),
        }

        return workflow_execution_repo.create(
            db,
            WorkflowExecutionCreate(
                workflow_id=source.workflow_id,
                trigger=f"resume:{execution_id}",
                priority=(
                    source.priority if priority is None else int(priority)
                ),
                parent_execution_id=execution_id,
                replay_of="resume_failed",
                input_data=payload,
            ),
        )

    def lineage(self, db: Session, execution_id: int) -> Dict[str, Any]:
        """Ancestors and direct children of an execution."""
        execution = workflow_execution_repo.get(db, execution_id)
        if not execution:
            raise NotFoundError(f"Execution {execution_id} not found.")

        ancestors: List[Dict[str, Any]] = []
        seen = {execution_id}
        cursor = execution
        while cursor.parent_execution_id and cursor.parent_execution_id not in seen:
            seen.add(cursor.parent_execution_id)
            parent = workflow_execution_repo.get(db, cursor.parent_execution_id)
            if not parent:
                break
            ancestors.append(serialize_execution(parent))
            cursor = parent

        children = (
            db.query(workflow_execution_repo.model)
            .filter(
                workflow_execution_repo.model.parent_execution_id == execution_id
            )
            .order_by(workflow_execution_repo.model.id)
            .all()
        )
        return {
            "execution_id": execution_id,
            "ancestors": ancestors,
            "children": [serialize_execution(child) for child in children],
        }


execution_history = ExecutionHistoryService()
