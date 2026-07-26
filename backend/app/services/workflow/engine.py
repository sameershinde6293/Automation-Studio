"""Asynchronous DAG workflow execution engine.

Fixes and features added in V1.1
--------------------------------
* **Busy-wait/deadlock bug fixed.** V1.0 spun the event loop at 100% CPU when
  ``tasks`` was empty but nodes remained; scheduling is now event-driven and a
  genuine stall raises immediately.
* **Cycle detection** up front via ``graph.validate_graph`` instead of a
  misleading "deadlock" error.
* **Per-node timeouts** so a hung node cannot hang the workflow forever.
* **Bounded concurrency** via a semaphore (``WORKFLOW_MAX_PARALLEL_NODES``).
* **Configurable retry policy** per node (``node.retry_policy``).
* **on_error policies**: ``fail`` (default), ``continue``, ``skip_branch``.
* **Progress events** published on the event bus for live UI updates.
* **Falsy outputs preserved** (V1.0 dropped ``{}``/``0``/``False`` results).
* **Checkpointing** into ``WorkflowExecution.state`` for resume/inspection.

Backwards compatible: ``WorkflowEngine.submit/cancel/run_execution`` and the
``workflow_engine`` singleton keep their signatures.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from app.core.errors import ExecutionError
from app.domain.models.workflow import (
    ExecutionPriority,
    ExecutionStatus,
    NodeExecution,
)
from app.domain.repositories.workflow_repository import (
    NodeExecutionCreate,
    edge_repo,
    node_execution_repo,
    node_repo,
    workflow_execution_repo,
    workflow_repo,
)
from app.infrastructure.config.settings import settings
from app.infrastructure.database.database import SessionLocal
from app.infrastructure.events.event_bus import event_bus
from app.infrastructure.logging.logger import get_logger

from .control import ControlHandle, control_registry
from .graph import (
    build_adjacency,
    descendants,
    is_loop_edge,
    loop_body,
    split_loop_edges,
    validate_graph,
    validate_graph_with_loops,
)
from .queue import ExecutionQueue, QueueFullError, WorkerPool, execution_queue
from .runtime import (
    NodeContext,
    NodeErrorCode,
    NodeMetrics,
    classify_exception,
    merge_metrics,
)
from .streaming import execution_broker
from . import streaming as stream_events

logger = get_logger("workflow")

# Event names published on the event bus.
EVENT_EXECUTION_STARTED = "workflow.execution.started"
EVENT_EXECUTION_FINISHED = "workflow.execution.finished"
EVENT_NODE_STARTED = "workflow.node.started"
EVENT_NODE_FINISHED = "workflow.node.finished"
EVENT_NODE_RETRY = "workflow.node.retry"
# M4 additions
EVENT_EXECUTION_QUEUED = "workflow.execution.queued"
EVENT_EXECUTION_PAUSED = "workflow.execution.paused"
EVENT_EXECUTION_RESUMED = "workflow.execution.resumed"
EVENT_NODE_SKIPPED = "workflow.node.skipped"

ON_ERROR_FAIL = "fail"
ON_ERROR_CONTINUE = "continue"
ON_ERROR_SKIP_BRANCH = "skip_branch"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class NodeFailure(Exception):
    """Internal signal: a node exhausted its retries."""

    def __init__(self, node_id: int, node_name: str, message: str) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.node_name = node_name
        self.message = message


class WorkflowEngine:
    def __init__(self, queue: Optional[ExecutionQueue] = None) -> None:
        self.active_tasks: Dict[int, asyncio.Task] = {}
        # Node status writes happen from a thread pool while many nodes run in
        # parallel. SQLite (and the get-or-create pattern below) is not safe
        # under concurrent inserts for the same row, so writes are serialised.
        # The lock is only held for the duration of a short DB transaction.
        self._write_lock = threading.Lock()
        # M4: bounded priority queue + worker pool. Each engine instance gets
        # its own queue when one is not supplied, so tests stay isolated.
        self._queue: ExecutionQueue = queue if queue is not None else ExecutionQueue()
        self._pool: Optional[WorkerPool] = None

    # ------------------------------------------------------------------ #
    # Task lifecycle
    # ------------------------------------------------------------------ #
    def submit(self, execution_id: int) -> asyncio.Task:
        """Schedule an execution on the running event loop."""
        existing = self.active_tasks.get(execution_id)
        if existing and not existing.done():
            logger.warning("Execution %s is already running.", execution_id)
            return existing

        task = asyncio.create_task(self.run_execution(execution_id))
        self.active_tasks[execution_id] = task
        task.add_done_callback(lambda _t, eid=execution_id: self.active_tasks.pop(eid, None))
        return task

    def cancel(self, execution_id: int) -> bool:
        """Request cancellation. Returns True if a live task was cancelled.

        M4: also removes the execution from the queue if it has not started, and
        signals its control handle so a paused run can unwind.
        """
        cancelled = False
        if self._queue.cancel(execution_id):
            cancelled = True
        handle = control_registry.get(execution_id)
        if handle is not None and handle.request_cancel():
            cancelled = True
        task = self.active_tasks.get(execution_id)
        if task and not task.done():
            task.cancel()
            cancelled = True
        return cancelled

    def is_running(self, execution_id: int) -> bool:
        task = self.active_tasks.get(execution_id)
        return bool(task and not task.done())

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Cancel all in-flight executions (called on app shutdown)."""
        tasks = [t for t in self.active_tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        self.active_tasks.clear()
        # M4: drain the queue and stop the worker pool, flushing pending logs.
        if self._pool is not None:
            await self._pool.shutdown(timeout=timeout)
            self._pool = None
        self._queue.clear()
        control_registry.clear()

    # ------------------------------------------------------------------ #
    # Persistence helpers (executed off the event loop via to_thread)
    # ------------------------------------------------------------------ #
    def _fetch_graph(self, execution_id: int):
        with SessionLocal() as db:
            execution = workflow_execution_repo.get(db, execution_id)
            if not execution:
                raise ValueError(f"Execution {execution_id} not found")
            nodes = node_repo.get_by_workflow(db, execution.workflow_id)
            edges = edge_repo.get_by_workflow(db, execution.workflow_id)
            # Detach lightweight snapshots so the ORM session can close safely.
            node_snapshots = [_NodeSnapshot.from_orm_node(n) for n in nodes]
            edge_pairs = [(e.source_id, e.target_id) for e in edges]
            return node_snapshots, edge_pairs

    def _fetch_execution_plan(self, execution_id: int):
        """Fetch nodes, labelled edges and run metadata in a single session.

        Replaces four separate session round-trips (bottleneck B2) with one.
        Returns ``(nodes, labelled_edges, run_meta)``.
        """
        with SessionLocal() as db:
            execution = workflow_execution_repo.get(db, execution_id)
            if not execution:
                raise ValueError(f"Execution {execution_id} not found")
            nodes = node_repo.get_by_workflow(db, execution.workflow_id)
            edges = edge_repo.get_by_workflow(db, execution.workflow_id)
            node_snapshots = [_NodeSnapshot.from_orm_node(n) for n in nodes]
            labelled = [(e.source_id, e.target_id, e.label) for e in edges]
            meta = {
                "workflow_id": execution.workflow_id,
                "input_data": dict(execution.input_data or {}),
                "priority": execution.priority,
                "trigger": execution.trigger,
                "replay_of": execution.replay_of,
                "state": dict(execution.state or {}),
                "status": execution.status,
            }
            return node_snapshots, labelled, meta

    def _finalise_execution(
        self,
        execution_id: int,
        status: ExecutionStatus,
        *,
        error: Optional[str] = None,
        state: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Single-transaction terminal write (status + state + metrics)."""
        with self._write_lock, SessionLocal() as db:
            execution = workflow_execution_repo.get(db, execution_id)
            if not execution:
                return
            execution.status = status
            if error is not None:
                execution.error = error[:4000]
            if state is not None:
                execution.state = state
            if metrics is not None:
                execution.metrics = metrics
            if status == ExecutionStatus.RUNNING and not execution.started_at:
                execution.started_at = _now_iso()
            if status.is_terminal:
                execution.finished_at = _now_iso()
            db.commit()

    def _persist_node_result(
        self,
        execution_id: int,
        node_id: int,
        status: ExecutionStatus,
        *,
        result: Any = None,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
        retry_count: Optional[int] = None,
        duration_ms: Optional[float] = None,
        queued_ms: Optional[float] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        iteration: Optional[int] = None,
        attempt_metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write a node's terminal state in one transaction.

        M1 issued a separate transaction per field group; this collapses the
        RUNNING/COMPLETED/checkpoint sequence into a single write per outcome
        (bottleneck B4/B5).
        """
        with self._write_lock, SessionLocal() as db:
            node_exec = node_execution_repo.get_by_execution_and_node(
                db, execution_id, node_id
            )
            if not node_exec:
                node_exec = node_execution_repo.create(
                    db,
                    NodeExecutionCreate(
                        execution_id=execution_id,
                        node_id=node_id,
                        status=status.value,
                        iteration=iteration or 0,
                    ),
                )
            node_exec.status = status
            if result is not None:
                node_exec.output_data = result
            if error is not None:
                node_exec.error = error[:4000]
            if error_code is not None:
                node_exec.error_code = error_code
            if retry_count is not None:
                node_exec.retry_count = retry_count
            if duration_ms is not None:
                node_exec.duration_ms = duration_ms
            if queued_ms is not None:
                node_exec.queued_ms = queued_ms
            if started_at is not None:
                node_exec.started_at = started_at
            if finished_at is not None:
                node_exec.finished_at = finished_at
            if iteration is not None:
                node_exec.iteration = iteration
            if attempt_metrics is not None:
                node_exec.attempt_metrics = attempt_metrics
            db.commit()

    def _reset_nodes_for_iteration(
        self, execution_id: int, node_ids: Sequence[int], iteration: int
    ) -> None:
        """Clear node executions so a loop body can run again."""
        if not node_ids:
            return
        with self._write_lock, SessionLocal() as db:
            rows = (
                db.query(NodeExecution)
                .filter(
                    NodeExecution.execution_id == execution_id,
                    NodeExecution.node_id.in_(list(node_ids)),
                )
                .all()
            )
            for row in rows:
                row.status = ExecutionStatus.PENDING
                row.error = None
                row.error_code = None
                row.iteration = iteration
            db.commit()

    def _update_execution_status(
        self,
        execution_id: int,
        status: ExecutionStatus,
        error: Optional[str] = None,
        state: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._write_lock, SessionLocal() as db:
            execution = workflow_execution_repo.get(db, execution_id)
            if not execution:
                return
            execution.status = status
            if error is not None:
                execution.error = error[:4000]
            if state is not None:
                execution.state = state
            if status == ExecutionStatus.RUNNING and not execution.started_at:
                execution.started_at = _now_iso()
            if status.is_terminal:
                execution.finished_at = _now_iso()
            db.commit()

    def _update_node_status(
        self,
        execution_id: int,
        node_id: int,
        status: ExecutionStatus,
        result: Any = None,
        error: Optional[str] = None,
        retry_count: Optional[int] = None,
        duration_ms: Optional[float] = None,
    ) -> None:
        with self._write_lock, SessionLocal() as db:
            node_exec = node_execution_repo.get_by_execution_and_node(
                db, execution_id, node_id
            )
            if not node_exec:
                node_exec = node_execution_repo.create(
                    db,
                    NodeExecutionCreate(
                        execution_id=execution_id, node_id=node_id, status=status.value
                    ),
                )
            node_exec.status = status
            # V1.0 bug: ``if result:`` dropped falsy outputs such as {} / 0.
            if result is not None:
                node_exec.output_data = result
            if error is not None:
                node_exec.error = error[:4000]
            if retry_count is not None:
                node_exec.retry_count = retry_count
            if duration_ms is not None:
                node_exec.duration_ms = duration_ms
            db.commit()

    def _checkpoint(self, execution_id: int, state: Dict[str, Any]) -> None:
        with self._write_lock, SessionLocal() as db:
            execution = workflow_execution_repo.get(db, execution_id)
            if execution:
                execution.state = state
                db.commit()

    # ------------------------------------------------------------------ #
    # Node execution
    # ------------------------------------------------------------------ #
    @staticmethod
    def _policy(node: "_NodeSnapshot") -> Dict[str, Any]:
        policy = node.retry_policy if isinstance(node.retry_policy, dict) else {}
        max_retries = policy.get("max_retries", settings.WORKFLOW_MAX_RETRIES)
        try:
            max_retries = max(1, min(int(max_retries), 10))
        except (TypeError, ValueError):
            max_retries = settings.WORKFLOW_MAX_RETRIES

        base_delay = policy.get("base_delay", settings.WORKFLOW_RETRY_BASE_DELAY)
        try:
            base_delay = max(0.0, min(float(base_delay), 60.0))
        except (TypeError, ValueError):
            base_delay = settings.WORKFLOW_RETRY_BASE_DELAY

        timeout = policy.get("timeout", settings.WORKFLOW_NODE_TIMEOUT_SECONDS)
        try:
            timeout = max(1.0, min(float(timeout), 86400.0))
        except (TypeError, ValueError):
            timeout = settings.WORKFLOW_NODE_TIMEOUT_SECONDS

        on_error = str(policy.get("on_error", ON_ERROR_FAIL)).lower()
        if on_error not in {ON_ERROR_FAIL, ON_ERROR_CONTINUE, ON_ERROR_SKIP_BRANCH}:
            on_error = ON_ERROR_FAIL

        return {
            "max_retries": max_retries,
            "base_delay": base_delay,
            "timeout": timeout,
            "on_error": on_error,
        }

    async def _run_node_with_retry(
        self,
        execution_id: int,
        node: "_NodeSnapshot",
        context: Dict[Any, Any],
        semaphore: asyncio.Semaphore,
    ) -> Tuple[int, Any]:
        from .executors import executor_registry

        policy = self._policy(node)
        max_retries = policy["max_retries"]
        base_delay = policy["base_delay"]
        timeout = policy["timeout"]

        async with semaphore:
            await asyncio.to_thread(
                self._update_node_status, execution_id, node.id, ExecutionStatus.RUNNING
            )
            event_bus.publish(
                EVENT_NODE_STARTED, execution_id=execution_id, node_id=node.id,
                node_name=node.name,
            )
            started = time.perf_counter()
            last_error = "unknown error"

            for attempt in range(max_retries):
                try:
                    executor = executor_registry.get_executor(node.node_type)
                    result = await asyncio.wait_for(
                        executor.execute(node, context), timeout=timeout
                    )
                    duration_ms = (time.perf_counter() - started) * 1000
                    await asyncio.to_thread(
                        self._update_node_status,
                        execution_id,
                        node.id,
                        ExecutionStatus.COMPLETED,
                        result,
                        None,
                        attempt,
                        duration_ms,
                    )
                    event_bus.publish(
                        EVENT_NODE_FINISHED,
                        execution_id=execution_id,
                        node_id=node.id,
                        node_name=node.name,
                        status=ExecutionStatus.COMPLETED.value,
                        duration_ms=duration_ms,
                    )
                    return node.id, result

                except asyncio.CancelledError:
                    await asyncio.to_thread(
                        self._update_node_status,
                        execution_id, node.id, ExecutionStatus.CANCELLED,
                    )
                    raise

                except asyncio.TimeoutError:
                    last_error = f"Node timed out after {timeout}s"
                    logger.warning(
                        "Node %s (%s) timed out on attempt %s/%s",
                        node.id, node.name, attempt + 1, max_retries,
                    )
                except Exception as exc:  # noqa: BLE001 - executor errors are data
                    last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "Node %s (%s) failed on attempt %s/%s: %s",
                        node.id, node.name, attempt + 1, max_retries, last_error,
                    )

                if attempt == max_retries - 1:
                    duration_ms = (time.perf_counter() - started) * 1000
                    await asyncio.to_thread(
                        self._update_node_status,
                        execution_id,
                        node.id,
                        ExecutionStatus.FAILED,
                        None,
                        last_error,
                        attempt + 1,
                        duration_ms,
                    )
                    event_bus.publish(
                        EVENT_NODE_FINISHED,
                        execution_id=execution_id,
                        node_id=node.id,
                        node_name=node.name,
                        status=ExecutionStatus.FAILED.value,
                        error=last_error,
                    )
                    raise NodeFailure(node.id, node.name, last_error)

                await asyncio.to_thread(
                    self._update_node_status,
                    execution_id,
                    node.id,
                    ExecutionStatus.RUNNING,
                    None,
                    f"Retry {attempt + 1}: {last_error}",
                    attempt + 1,
                )
                event_bus.publish(
                    EVENT_NODE_RETRY,
                    execution_id=execution_id,
                    node_id=node.id,
                    attempt=attempt + 1,
                    error=last_error,
                )
                await asyncio.sleep(base_delay * (2 ** attempt))

            # Unreachable, but keeps type checkers happy.
            raise NodeFailure(node.id, node.name, last_error)

    # ------------------------------------------------------------------ #
    # Main scheduler
    # ------------------------------------------------------------------ #
    async def run_execution(self, execution_id: int) -> Dict[str, Any]:
        """Run one workflow execution to completion.

        Returns a summary dict; never raises for ordinary node failures (they
        are reflected in the persisted execution status).
        """
        try:
            nodes, edge_pairs = await asyncio.to_thread(self._fetch_graph, execution_id)
        except Exception as exc:
            logger.error("Failed to fetch graph for %s: %s", execution_id, exc)
            return {"execution_id": execution_id, "status": "ERROR", "error": str(exc)}

        node_ids = [n.id for n in nodes]
        node_by_id = {n.id: n for n in nodes}

        validation = validate_graph(
            node_ids, edge_pairs, max_nodes=settings.WORKFLOW_MAX_NODES
        )
        if not validation.is_valid:
            message = "; ".join(validation.errors)
            await asyncio.to_thread(
                self._update_execution_status, execution_id, ExecutionStatus.FAILED, message
            )
            logger.error("Execution %s rejected: %s", execution_id, message)
            event_bus.publish(
                EVENT_EXECUTION_FINISHED,
                execution_id=execution_id,
                status=ExecutionStatus.FAILED.value,
                error=message,
            )
            return {"execution_id": execution_id, "status": "FAILED", "error": message}

        for warning in validation.warnings:
            logger.info("Execution %s: %s", execution_id, warning)

        dependencies, _dependents = build_adjacency(node_ids, edge_pairs)

        completed: Set[int] = set()
        failed: Set[int] = set()
        skipped: Set[int] = set()
        running: Set[int] = set()
        tasks: Dict[asyncio.Task, int] = {}
        context: Dict[Any, Any] = {}
        errors: List[str] = []
        fail_fast = False

        max_parallel = max(1, settings.WORKFLOW_MAX_PARALLEL_NODES)
        semaphore = asyncio.Semaphore(max_parallel)

        await asyncio.to_thread(
            self._update_execution_status, execution_id, ExecutionStatus.RUNNING
        )
        event_bus.publish(
            EVENT_EXECUTION_STARTED, execution_id=execution_id, node_count=len(nodes)
        )
        logger.info("Execution %s STARTED (%s nodes)", execution_id, len(nodes))
        started_at = time.perf_counter()

        try:
            while True:
                settled = completed | failed | skipped
                if len(settled) >= len(nodes) and not tasks:
                    break

                if not fail_fast:
                    ready = [
                        node_by_id[nid]
                        for nid in node_ids
                        if nid not in settled
                        and nid not in running
                        and dependencies[nid].issubset(completed | skipped)
                    ]
                    for node in ready:
                        running.add(node.id)
                        task = asyncio.create_task(
                            self._run_node_with_retry(execution_id, node, context, semaphore)
                        )
                        tasks[task] = node.id

                if not tasks:
                    if fail_fast or len(settled) >= len(nodes):
                        break
                    # Nothing runnable and nothing running: the remaining nodes
                    # depend on failed nodes. Mark them skipped rather than
                    # spinning the loop (the V1.0 busy-wait bug).
                    stalled = [nid for nid in node_ids if nid not in settled]
                    for nid in stalled:
                        skipped.add(nid)
                        await asyncio.to_thread(
                            self._update_node_status,
                            execution_id, nid, ExecutionStatus.SKIPPED,
                            None, "Upstream dependency did not complete.",
                        )
                    logger.info(
                        "Execution %s: skipped %s unreachable node(s).",
                        execution_id, len(stalled),
                    )
                    break

                done, _pending = await asyncio.wait(
                    tasks.keys(), return_when=asyncio.FIRST_COMPLETED
                )

                for task in done:
                    node_id = tasks.pop(task)
                    running.discard(node_id)
                    node = node_by_id[node_id]

                    if task.cancelled():
                        raise asyncio.CancelledError()

                    exc = task.exception()
                    if exc is None:
                        _nid, result = task.result()
                        completed.add(node_id)
                        context[node_id] = result
                        if node.name:
                            context.setdefault(node.name, result)
                        continue

                    if isinstance(exc, asyncio.CancelledError):
                        raise exc

                    message = (
                        exc.message if isinstance(exc, NodeFailure) else f"{type(exc).__name__}: {exc}"
                    )
                    failed.add(node_id)
                    errors.append(f"[{node.name or node_id}] {message}")
                    policy = self._policy(node)

                    if policy["on_error"] == ON_ERROR_CONTINUE:
                        # Treat as settled so downstream nodes may still run.
                        completed.add(node_id)
                        failed.discard(node_id)
                        context[node_id] = {"error": message, "failed": True}
                        logger.info(
                            "Node %s failed but on_error=continue; proceeding.", node_id
                        )
                    elif policy["on_error"] == ON_ERROR_SKIP_BRANCH:
                        for descendant in descendants(node_id, edge_pairs):
                            if descendant not in completed and descendant not in skipped:
                                skipped.add(descendant)
                                await asyncio.to_thread(
                                    self._update_node_status,
                                    execution_id, descendant, ExecutionStatus.SKIPPED,
                                    None, f"Skipped: upstream node {node_id} failed.",
                                )
                        logger.info(
                            "Node %s failed; skipping its downstream branch.", node_id
                        )
                    else:
                        fail_fast = True
                        logger.error("Node %s failed; failing the workflow.", node_id)

                await asyncio.to_thread(
                    self._checkpoint,
                    execution_id,
                    {
                        "completed": sorted(completed),
                        "failed": sorted(failed),
                        "skipped": sorted(skipped),
                        "updated_at": _now_iso(),
                    },
                )

                if fail_fast:
                    for pending_task in list(tasks.keys()):
                        pending_task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks.keys(), return_exceptions=True)
                    tasks.clear()
                    running.clear()
                    break

            duration_ms = (time.perf_counter() - started_at) * 1000
            final_state = {
                "completed": sorted(completed),
                "failed": sorted(failed),
                "skipped": sorted(skipped),
                "duration_ms": round(duration_ms, 2),
                "updated_at": _now_iso(),
            }

            if failed:
                message = "; ".join(errors)[:4000]
                await asyncio.to_thread(
                    self._update_execution_status,
                    execution_id, ExecutionStatus.FAILED, message, final_state,
                )
                logger.error("Execution %s FAILED: %s", execution_id, message)
                status = ExecutionStatus.FAILED
            else:
                await asyncio.to_thread(
                    self._update_execution_status,
                    execution_id, ExecutionStatus.COMPLETED, None, final_state,
                )
                logger.info(
                    "Execution %s COMPLETED in %.1fms", execution_id, duration_ms
                )
                status = ExecutionStatus.COMPLETED

            event_bus.publish(
                EVENT_EXECUTION_FINISHED,
                execution_id=execution_id,
                status=status.value,
                duration_ms=duration_ms,
            )
            return {
                "execution_id": execution_id,
                "status": status.value,
                "completed": sorted(completed),
                "failed": sorted(failed),
                "skipped": sorted(skipped),
                "duration_ms": round(duration_ms, 2),
                "errors": errors,
            }

        except asyncio.CancelledError:
            for task in list(tasks.keys()):
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks.keys(), return_exceptions=True)
            await asyncio.to_thread(
                self._update_execution_status, execution_id, ExecutionStatus.CANCELLED
            )
            event_bus.publish(
                EVENT_EXECUTION_FINISHED,
                execution_id=execution_id,
                status=ExecutionStatus.CANCELLED.value,
            )
            logger.info("Execution %s CANCELLED", execution_id)
            return {"execution_id": execution_id, "status": "CANCELLED"}

        except Exception as exc:  # noqa: BLE001 - engine must never crash the app
            for task in list(tasks.keys()):
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks.keys(), return_exceptions=True)
            message = f"{type(exc).__name__}: {exc}"
            await asyncio.to_thread(
                self._update_execution_status,
                execution_id, ExecutionStatus.FAILED, message,
            )
            event_bus.publish(
                EVENT_EXECUTION_FINISHED,
                execution_id=execution_id,
                status=ExecutionStatus.FAILED.value,
                error=message,
            )
            logger.exception("Execution %s FAILED unexpectedly", execution_id)
            return {"execution_id": execution_id, "status": "FAILED", "error": message}

    # ------------------------------------------------------------------ #
    # M4: queue-aware submission and execution control
    # ------------------------------------------------------------------ #
    def start_workers(self) -> bool:
        """Start the execution worker pool. Called from the app lifespan.

        Deliberately *not* called from request handlers: workers are long-lived
        tasks, and creating them inside a request scope leaks tasks into that
        request's lifetime (and hangs test clients that never run the lifespan).
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("start_workers() called without a running event loop.")
            return False
        if self._pool is None:
            self._pool = WorkerPool(self._queue, self.run_execution_v2)
        if not self._pool.is_running:
            self._pool.start()
        execution_broker.bind_loop()
        return self._pool.is_running

    @property
    def workers_running(self) -> bool:
        return bool(self._pool is not None and self._pool.is_running)

    def submit_v2(self, execution_id: int) -> asyncio.Task:
        """Run an execution directly on the current loop via the M4 scheduler."""
        existing = self.active_tasks.get(execution_id)
        if existing and not existing.done():
            logger.warning("Execution %s is already running.", execution_id)
            return existing
        task = asyncio.create_task(self.run_execution_v2(execution_id))
        self.active_tasks[execution_id] = task
        task.add_done_callback(lambda _t, eid=execution_id: self.active_tasks.pop(eid, None))
        return task

    def enqueue(
        self,
        execution_id: int,
        *,
        priority: int = ExecutionPriority.NORMAL.value,
        workflow_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Admit an execution for running.

        When the worker pool is up (normal server operation) the run enters the
        bounded priority queue and :class:`QueueFullError` (HTTP 429) is raised
        at capacity, instead of the unbounded task spawning M1 did.

        When no pool is running — an embedded/test host that never executed the
        application lifespan — the run is submitted directly so behaviour
        matches pre-M4. The caller can tell which happened from ``mode``.
        """
        control_registry.get_or_create(execution_id)
        execution_broker.bind_loop()

        if not self.workers_running:
            self.submit_v2(execution_id)
            return {
                "execution_id": execution_id,
                "mode": "direct",
                "status": ExecutionStatus.PENDING.value,
                "priority": priority,
                "position": 0,
                "queue_size": self._queue.size(),
            }

        item = self._queue.put(
            execution_id, priority=priority, workflow_id=workflow_id
        )
        self._mark_queued(execution_id)
        position = self._queue.position(execution_id) or 0
        execution_broker.publish(
            execution_id,
            stream_events.EVENT_EXECUTION_QUEUED,
            priority=priority,
            position=position,
            workflow_id=workflow_id,
        )
        event_bus.publish(
            EVENT_EXECUTION_QUEUED,
            execution_id=execution_id,
            priority=priority,
            position=position,
        )
        return {
            "execution_id": execution_id,
            "mode": "queued",
            "status": ExecutionStatus.QUEUED.value,
            "priority": item.priority,
            "position": position,
            "queue_size": self._queue.size(),
        }

    def _mark_queued(self, execution_id: int) -> None:
        try:
            with self._write_lock, SessionLocal() as db:
                execution = workflow_execution_repo.get(db, execution_id)
                if execution and not execution.status.is_terminal:
                    execution.status = ExecutionStatus.QUEUED
                    execution.queued_at = _now_iso()
                    db.commit()
        except Exception:
            logger.debug("Could not mark execution %s queued", execution_id, exc_info=True)

    # -- control operations ------------------------------------------------ #
    def pause(self, execution_id: int) -> bool:
        """Pause a running execution. In-flight nodes finish first."""
        handle = control_registry.get(execution_id)
        if handle is None or not handle.request_pause():
            return False
        try:
            self._update_execution_status(execution_id, ExecutionStatus.PAUSING)
        except Exception:  # pragma: no cover - status write is best effort
            logger.debug("Could not persist PAUSING for %s", execution_id, exc_info=True)
        execution_broker.publish(execution_id, stream_events.EVENT_EXECUTION_PAUSED)
        execution_broker.log(execution_id, "Pause requested.", level="INFO")
        event_bus.publish(EVENT_EXECUTION_PAUSED, execution_id=execution_id)
        return True

    def resume(self, execution_id: int) -> bool:
        """Resume a paused execution."""
        handle = control_registry.get(execution_id)
        if handle is None or not handle.request_resume():
            return False
        try:
            self._update_execution_status(execution_id, ExecutionStatus.RUNNING)
        except Exception:  # pragma: no cover
            logger.debug("Could not persist RUNNING for %s", execution_id, exc_info=True)
        execution_broker.publish(execution_id, stream_events.EVENT_EXECUTION_RESUMED)
        execution_broker.log(execution_id, "Execution resumed.", level="INFO")
        event_bus.publish(EVENT_EXECUTION_RESUMED, execution_id=execution_id)
        return True

    def stop(self, execution_id: int) -> bool:
        """Graceful stop: drain in-flight nodes then finish as CANCELLED."""
        if self._queue.cancel(execution_id):
            # Never started; terminate immediately.
            self._update_execution_status(execution_id, ExecutionStatus.CANCELLED)
            execution_broker.publish(
                execution_id,
                stream_events.EVENT_EXECUTION_FINISHED,
                status=ExecutionStatus.CANCELLED.value,
                reason="stopped while queued",
            )
            control_registry.release(execution_id)
            return True
        handle = control_registry.get(execution_id)
        if handle is None or not handle.request_stop():
            return False
        try:
            self._update_execution_status(execution_id, ExecutionStatus.STOPPING)
        except Exception:  # pragma: no cover
            logger.debug("Could not persist STOPPING for %s", execution_id, exc_info=True)
        execution_broker.publish(execution_id, stream_events.EVENT_EXECUTION_STOPPING)
        execution_broker.log(execution_id, "Graceful stop requested.", level="WARNING")
        return True

    def is_paused(self, execution_id: int) -> bool:
        handle = control_registry.get(execution_id)
        return bool(handle and handle.is_paused)

    def queue_status(self) -> Dict[str, Any]:
        """Queue depth, waiting entries and worker pool stats."""
        pool_stats = self._pool.stats() if self._pool else {
            "workers": max(1, settings.EXECUTION_MAX_WORKERS),
            "running": False,
            "active": 0,
            "active_executions": [],
            "completed": 0,
            "failed": 0,
        }
        return {
            "queue_size": self._queue.size(),
            "queue_max_size": settings.EXECUTION_QUEUE_MAX_SIZE,
            "queued": self._queue.snapshot(),
            "workers": pool_stats,
            "running_executions": sorted(
                eid for eid, task in self.active_tasks.items() if not task.done()
            ),
            "paused_executions": [
                eid
                for eid in control_registry.active_ids()
                if self.is_paused(eid)
            ],
            "streaming": execution_broker.stats(),
        }

    # ------------------------------------------------------------------ #
    # M4: scheduler with branch gating, loops, pause/resume and metrics
    # ------------------------------------------------------------------ #
    async def run_execution_v2(self, execution_id: int) -> Dict[str, Any]:
        """Execute a workflow with the full M4 feature set.

        Differences from :meth:`run_execution` (which is preserved verbatim for
        the M1 contract and its tests):

        * follows only the branch labels a condition node activates;
        * supports bounded loops via ``loop``-labelled back-edges;
        * honours pause/resume/stop through a :class:`ControlHandle`;
        * seeds run variables from ``WorkflowExecution.input_data``;
        * streams live events and durable logs;
        * records per-node and aggregate metrics.
        """
        handle = control_registry.get_or_create(execution_id)
        handle.bind_loop()
        execution_broker.bind_loop()

        try:
            nodes, labelled_edges, meta = await asyncio.to_thread(
                self._fetch_execution_plan, execution_id
            )
        except Exception as exc:
            logger.error("Failed to load execution %s: %s", execution_id, exc)
            control_registry.release(execution_id)
            return {"execution_id": execution_id, "status": "ERROR", "error": str(exc)}

        node_ids = [n.id for n in nodes]
        node_by_id = {n.id: n for n in nodes}

        validation, forward_edges, loop_edges = validate_graph_with_loops(
            node_ids, labelled_edges, max_nodes=settings.WORKFLOW_MAX_NODES
        )
        if not validation.is_valid:
            message = "; ".join(validation.errors)
            await asyncio.to_thread(
                self._finalise_execution,
                execution_id,
                ExecutionStatus.FAILED,
                error=message,
            )
            execution_broker.log(execution_id, f"Rejected: {message}", level="ERROR")
            execution_broker.publish(
                execution_id,
                stream_events.EVENT_EXECUTION_FINISHED,
                status=ExecutionStatus.FAILED.value,
                error=message,
            )
            execution_broker.finish(execution_id)
            event_bus.publish(
                EVENT_EXECUTION_FINISHED,
                execution_id=execution_id,
                status=ExecutionStatus.FAILED.value,
                error=message,
            )
            control_registry.release(execution_id)
            return {"execution_id": execution_id, "status": "FAILED", "error": message}

        for warning in validation.warnings:
            execution_broker.log(execution_id, warning, level="WARNING")

        # Outgoing labelled edges per node, for branch gating.
        outgoing: Dict[int, List[Tuple[int, Optional[str]]]] = {n: [] for n in node_ids}
        for source, target, label in labelled_edges:
            if is_loop_edge(label):
                continue
            if source in outgoing:
                outgoing[source].append((target, label))

        dependencies, dependents = build_adjacency(node_ids, forward_edges)

        context = NodeContext(
            execution_id=execution_id,
            workflow_id=meta.get("workflow_id"),
            variables=meta.get("input_data") or {},
        )
        context.cancel_event = handle.stop_event
        context.log_sink = lambda message, level="INFO": execution_broker.log(
            execution_id, message, level=level
        )

        completed: Set[int] = set()
        failed: Set[int] = set()
        skipped: Set[int] = set()
        running: Set[int] = set()
        tasks: Dict[asyncio.Task, int] = {}
        errors: List[str] = []
        aggregate: Dict[str, Any] = {}
        iterations: Dict[Tuple[int, int], int] = {}
        node_run_count = 0
        fail_fast = False
        stopped_early = False
        #: Set when the engine itself aborts the run (wall-clock timeout or the
        #: node-execution cap) as opposed to a node failing. Without this the
        #: run could finish with an error recorded but still report COMPLETED,
        #: because the terminal status only inspected ``failed``.
        abort_reason: Optional[str] = None

        max_parallel = max(1, settings.WORKFLOW_MAX_PARALLEL_NODES)
        semaphore = asyncio.Semaphore(max_parallel)

        await asyncio.to_thread(
            self._update_execution_status, execution_id, ExecutionStatus.RUNNING
        )
        execution_broker.publish(
            execution_id,
            stream_events.EVENT_EXECUTION_STARTED,
            node_count=len(nodes),
            workflow_id=meta.get("workflow_id"),
        )
        execution_broker.log(
            execution_id, f"Execution started with {len(nodes)} node(s).", level="INFO"
        )
        event_bus.publish(
            EVENT_EXECUTION_STARTED, execution_id=execution_id, node_count=len(nodes)
        )
        started_at = time.perf_counter()
        deadline = started_at + max(1.0, settings.EXECUTION_TIMEOUT_SECONDS)

        def emit_progress() -> None:
            total = len(nodes)
            done = len(completed | failed | skipped)
            execution_broker.publish(
                execution_id,
                stream_events.EVENT_EXECUTION_PROGRESS,
                completed=len(completed),
                failed=len(failed),
                skipped=len(skipped),
                running=sorted(running),
                total=total,
                percent=round((done / total) * 100, 1) if total else 100.0,
            )

        def gated_out(node_id: int) -> Set[int]:
            """Targets suppressed by the branch decision of ``node_id``."""
            chosen = branch_choices.get(node_id)
            if chosen is None:
                return set()
            blocked = set()
            for target, label in outgoing.get(node_id, ()):
                if label and str(label).strip() and str(label).strip() not in chosen:
                    blocked.add(target)
            return blocked

        branch_choices: Dict[int, Set[str]] = {}
        suppressed: Set[int] = set()

        def recompute_suppressed() -> None:
            """Nodes whose only inbound path was cut by a branch decision."""
            newly: Set[int] = set()
            for decider, _choice in branch_choices.items():
                for target in gated_out(decider):
                    if target in completed or target in running:
                        continue
                    # Suppress only when *every* inbound forward edge is either
                    # blocked or comes from a suppressed/skipped node.
                    inbound = [
                        source
                        for source, tgt in forward_edges
                        if tgt == target
                    ]
                    if not inbound:
                        continue
                    if all(
                        (src in branch_choices and target in gated_out(src))
                        or src in suppressed
                        or src in skipped
                        for src in inbound
                    ):
                        newly.add(target)
            if newly - suppressed:
                suppressed.update(newly)
                # Cascade to descendants that become unreachable.
                for node_id in list(newly):
                    for child in descendants(node_id, forward_edges):
                        if child in completed or child in running:
                            continue
                        inbound = [s for s, t in forward_edges if t == child]
                        if inbound and all(
                            src in suppressed or src in skipped for src in inbound
                        ):
                            suppressed.add(child)

        try:
            while True:
                if time.perf_counter() > deadline and abort_reason is None:
                    abort_reason = (
                        f"Execution exceeded the {settings.EXECUTION_TIMEOUT_SECONDS}s "
                        "wall-clock timeout."
                    )
                    errors.append(abort_reason)
                    fail_fast = True

                # --- pause -------------------------------------------------
                if handle.is_paused and not tasks:
                    await asyncio.to_thread(
                        self._update_execution_status,
                        execution_id,
                        ExecutionStatus.PAUSED,
                    )
                    execution_broker.log(execution_id, "Execution paused.", level="INFO")
                    await handle.wait_if_paused(settings.EXECUTION_PAUSE_POLL_SECONDS)
                    if not handle.should_halt:
                        await asyncio.to_thread(
                            self._update_execution_status,
                            execution_id,
                            ExecutionStatus.RUNNING,
                        )
                    continue

                if handle.is_cancelled:
                    raise asyncio.CancelledError()

                if handle.is_stopping and not fail_fast:
                    stopped_early = True

                settled = completed | failed | skipped | suppressed
                if len(settled) >= len(nodes) and not tasks:
                    break

                schedule_new = not fail_fast and not stopped_early and not handle.is_paused
                if schedule_new:
                    ready = [
                        node_by_id[nid]
                        for nid in node_ids
                        if nid not in settled
                        and nid not in running
                        and dependencies[nid].issubset(completed | skipped | suppressed)
                        and not any(
                            nid in gated_out(dep)
                            for dep in dependencies[nid]
                            if dep in branch_choices
                        )
                    ]
                    for node in ready:
                        if node_run_count >= settings.WORKFLOW_MAX_NODE_EXECUTIONS:
                            abort_reason = (
                                "Execution exceeded WORKFLOW_MAX_NODE_EXECUTIONS "
                                f"({settings.WORKFLOW_MAX_NODE_EXECUTIONS})."
                            )
                            errors.append(abort_reason)
                            fail_fast = True
                            break
                        node_run_count += 1
                        running.add(node.id)
                        iteration = iterations.get((node.id, 0), 0)
                        task = asyncio.create_task(
                            self._run_node_v2(
                                execution_id, node, context, semaphore, handle, iteration
                            )
                        )
                        tasks[task] = node.id
                    if ready:
                        emit_progress()

                if not tasks:
                    if fail_fast or stopped_early or len(settled) >= len(nodes):
                        break
                    stalled = [nid for nid in node_ids if nid not in settled]
                    if not stalled:
                        break
                    for nid in stalled:
                        skipped.add(nid)
                        await asyncio.to_thread(
                            self._persist_node_result,
                            execution_id,
                            nid,
                            ExecutionStatus.SKIPPED,
                            error="Upstream dependency did not complete.",
                        )
                        execution_broker.publish(
                            execution_id,
                            stream_events.EVENT_NODE_SKIPPED,
                            node_id=nid,
                            reason="unreachable",
                        )
                    execution_broker.log(
                        execution_id,
                        f"Skipped {len(stalled)} unreachable node(s).",
                        level="WARNING",
                    )
                    break

                done, _pending = await asyncio.wait(
                    tasks.keys(), return_when=asyncio.FIRST_COMPLETED, timeout=1.0
                )
                if not done:
                    continue

                for task in done:
                    node_id = tasks.pop(task)
                    running.discard(node_id)
                    node = node_by_id[node_id]

                    if task.cancelled():
                        raise asyncio.CancelledError()

                    exc = task.exception()
                    if exc is None:
                        _nid, result, metrics, branches = task.result()
                        completed.add(node_id)
                        context.record_output(node_id, node.name, result)
                        merge_metrics(aggregate, metrics)
                        if branches:
                            branch_choices[node_id] = set(branches)
                            recompute_suppressed()
                        continue

                    if isinstance(exc, asyncio.CancelledError):
                        raise exc

                    message = (
                        exc.message
                        if isinstance(exc, NodeFailure)
                        else f"{type(exc).__name__}: {exc}"
                    )
                    failed.add(node_id)
                    errors.append(f"[{node.name or node_id}] {message}")
                    policy = self._policy(node)

                    if policy["on_error"] == ON_ERROR_CONTINUE:
                        completed.add(node_id)
                        failed.discard(node_id)
                        context.record_output(
                            node_id, node.name, {"error": message, "failed": True}
                        )
                        execution_broker.log(
                            execution_id,
                            f"Node {node.name or node_id} failed but on_error=continue.",
                            level="WARNING",
                            node_id=node_id,
                        )
                    elif policy["on_error"] == ON_ERROR_SKIP_BRANCH:
                        for descendant in descendants(node_id, forward_edges):
                            if descendant not in completed and descendant not in skipped:
                                skipped.add(descendant)
                                await asyncio.to_thread(
                                    self._persist_node_result,
                                    execution_id,
                                    descendant,
                                    ExecutionStatus.SKIPPED,
                                    error=f"Skipped: upstream node {node_id} failed.",
                                )
                        execution_broker.log(
                            execution_id,
                            f"Node {node.name or node_id} failed; skipping its branch.",
                            level="WARNING",
                            node_id=node_id,
                        )
                    else:
                        fail_fast = True
                        execution_broker.log(
                            execution_id,
                            f"Node {node.name or node_id} failed; failing the workflow.",
                            level="ERROR",
                            node_id=node_id,
                        )

                emit_progress()

                # --- loop back-edges --------------------------------------
                if loop_edges and not fail_fast and not stopped_early:
                    for source, target, label in loop_edges:
                        if source not in completed or target not in completed:
                            continue
                        key = (source, target)
                        count = iterations.get(key, 0)
                        limit = self._loop_limit(node_by_id.get(target))
                        if count >= limit:
                            continue
                        if not self._loop_should_continue(context, node_by_id.get(source)):
                            continue
                        iterations[key] = count + 1
                        body = loop_body(target, source, forward_edges)
                        for nid in body:
                            completed.discard(nid)
                            failed.discard(nid)
                            skipped.discard(nid)
                            suppressed.discard(nid)
                            branch_choices.pop(nid, None)
                        await asyncio.to_thread(
                            self._reset_nodes_for_iteration,
                            execution_id,
                            sorted(body),
                            count + 1,
                        )
                        execution_broker.log(
                            execution_id,
                            f"Loop iteration {count + 1} over {len(body)} node(s).",
                            level="INFO",
                        )

                if fail_fast or stopped_early:
                    for pending_task in list(tasks.keys()):
                        if stopped_early and not fail_fast:
                            continue  # graceful: let in-flight nodes finish
                        pending_task.cancel()
                    if tasks and fail_fast:
                        await asyncio.gather(*tasks.keys(), return_exceptions=True)
                        tasks.clear()
                        running.clear()
                    if fail_fast:
                        break

            duration_ms = (time.perf_counter() - started_at) * 1000
            aggregate["duration_ms"] = round(duration_ms, 2)
            aggregate["paused_seconds"] = handle.total_paused_seconds
            aggregate["loop_iterations"] = sum(iterations.values())
            final_state = {
                "completed": sorted(completed),
                "failed": sorted(failed),
                "skipped": sorted(skipped | suppressed),
                "duration_ms": round(duration_ms, 2),
                "variables": context.snapshot_variables(),
                "updated_at": _now_iso(),
            }

            if failed or abort_reason:
                status = ExecutionStatus.FAILED
                message = "; ".join(errors)[:4000]
            elif stopped_early or handle.is_stopping:
                status = ExecutionStatus.CANCELLED
                message = "Execution stopped by request."
            else:
                status = ExecutionStatus.COMPLETED
                message = None

            await asyncio.to_thread(
                self._finalise_execution,
                execution_id,
                status,
                error=message,
                state=final_state,
                metrics=aggregate,
            )
            execution_broker.log(
                execution_id,
                f"Execution {status.value} in {duration_ms:.1f}ms.",
                level="ERROR" if status == ExecutionStatus.FAILED else "INFO",
                flush=True,
            )
            execution_broker.publish(
                execution_id,
                stream_events.EVENT_EXECUTION_FINISHED,
                status=status.value,
                duration_ms=round(duration_ms, 2),
                metrics=aggregate,
                error=message,
            )
            execution_broker.finish(execution_id)
            event_bus.publish(
                EVENT_EXECUTION_FINISHED,
                execution_id=execution_id,
                status=status.value,
                duration_ms=duration_ms,
            )
            control_registry.release(execution_id)
            return {
                "execution_id": execution_id,
                "status": status.value,
                "completed": sorted(completed),
                "failed": sorted(failed),
                "skipped": sorted(skipped | suppressed),
                "duration_ms": round(duration_ms, 2),
                "metrics": aggregate,
                "errors": errors,
            }

        except asyncio.CancelledError:
            for task in list(tasks.keys()):
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks.keys(), return_exceptions=True)
            await asyncio.to_thread(
                self._finalise_execution,
                execution_id,
                ExecutionStatus.CANCELLED,
                metrics=aggregate or None,
            )
            execution_broker.log(
                execution_id, "Execution cancelled.", level="WARNING", flush=True
            )
            execution_broker.publish(
                execution_id,
                stream_events.EVENT_EXECUTION_FINISHED,
                status=ExecutionStatus.CANCELLED.value,
            )
            execution_broker.finish(execution_id)
            event_bus.publish(
                EVENT_EXECUTION_FINISHED,
                execution_id=execution_id,
                status=ExecutionStatus.CANCELLED.value,
            )
            control_registry.release(execution_id)
            return {"execution_id": execution_id, "status": "CANCELLED"}

        except Exception as exc:  # noqa: BLE001 - engine must never crash the app
            for task in list(tasks.keys()):
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks.keys(), return_exceptions=True)
            message = f"{type(exc).__name__}: {exc}"
            await asyncio.to_thread(
                self._finalise_execution,
                execution_id,
                ExecutionStatus.FAILED,
                error=message,
            )
            execution_broker.log(execution_id, message, level="ERROR", flush=True)
            execution_broker.publish(
                execution_id,
                stream_events.EVENT_EXECUTION_FINISHED,
                status=ExecutionStatus.FAILED.value,
                error=message,
            )
            execution_broker.finish(execution_id)
            event_bus.publish(
                EVENT_EXECUTION_FINISHED,
                execution_id=execution_id,
                status=ExecutionStatus.FAILED.value,
                error=message,
            )
            logger.exception("Execution %s FAILED unexpectedly", execution_id)
            control_registry.release(execution_id)
            return {"execution_id": execution_id, "status": "FAILED", "error": message}

    # -- loop helpers ------------------------------------------------------ #
    @staticmethod
    def _loop_limit(node: Optional["_NodeSnapshot"]) -> int:
        """Iteration cap for a loop, from the loop node's config."""
        cap = settings.WORKFLOW_MAX_LOOP_ITERATIONS
        config = getattr(node, "config", None) if node else None
        if isinstance(config, dict):
            requested = config.get("max_iterations")
            if requested:
                try:
                    cap = min(cap, max(1, int(requested)))
                except (TypeError, ValueError):
                    pass
        return cap

    @staticmethod
    def _loop_should_continue(
        context: Dict[Any, Any], node: Optional["_NodeSnapshot"]
    ) -> bool:
        """Whether a loop back-edge should fire another iteration.

        When the node closing the loop is a condition/branch node, its decision
        governs continuation: the loop repeats only while it evaluates true.
        Otherwise the loop repeats until the iteration cap is reached.
        """
        if node is None:
            return True
        output = context.get(node.id)
        if isinstance(output, dict) and "result" in output:
            value = output.get("result")
            if isinstance(value, bool):
                return value
        return True

    async def _run_node_v2(
        self,
        execution_id: int,
        node: "_NodeSnapshot",
        context: NodeContext,
        semaphore: asyncio.Semaphore,
        handle: ControlHandle,
        iteration: int = 0,
    ) -> Tuple[int, Any, NodeMetrics, Optional[List[str]]]:
        """Run one node with retries, metrics and error classification."""
        from .executors import executor_registry

        policy = self._policy(node)
        max_retries = policy["max_retries"]
        base_delay = policy["base_delay"]
        timeout = policy["timeout"]

        queue_started = time.perf_counter()
        async with semaphore:
            queued_ms = (time.perf_counter() - queue_started) * 1000
            started_iso = _now_iso()
            started = time.perf_counter()
            attempts: List[Dict[str, Any]] = []
            last_error = "unknown error"
            last_code = NodeErrorCode.UNKNOWN

            await asyncio.to_thread(
                self._persist_node_result,
                execution_id,
                node.id,
                ExecutionStatus.RUNNING,
                queued_ms=queued_ms,
                started_at=started_iso,
                iteration=iteration,
            )
            execution_broker.publish(
                execution_id,
                stream_events.EVENT_NODE_STARTED,
                node_id=node.id,
                node_name=node.name,
                node_type=node.node_type,
                iteration=iteration,
            )
            event_bus.publish(
                EVENT_NODE_STARTED,
                execution_id=execution_id,
                node_id=node.id,
                node_name=node.name,
            )

            for attempt in range(max_retries):
                attempt_started = time.perf_counter()
                try:
                    executor = executor_registry.get_executor(node.node_type)
                    context.pop("__last_metrics__", None)
                    context.pop("__last_branches__", None)
                    result = await asyncio.wait_for(
                        executor.execute(node, context), timeout=timeout
                    )
                    duration_ms = (time.perf_counter() - started) * 1000

                    metrics = context.pop("__last_metrics__", None)
                    if not isinstance(metrics, NodeMetrics):
                        metrics = NodeMetrics(duration_ms=duration_ms)
                    metrics.duration_ms = metrics.duration_ms or duration_ms
                    metrics.queued_ms = queued_ms
                    metrics.attempts = attempt + 1
                    branches = context.pop("__last_branches__", None)
                    if branches is None and isinstance(result, dict) and "branch" in result:
                        branches = [str(result["branch"])]

                    attempts.append(
                        {
                            "attempt": attempt + 1,
                            "ok": True,
                            "duration_ms": round(
                                (time.perf_counter() - attempt_started) * 1000, 3
                            ),
                        }
                    )
                    await asyncio.to_thread(
                        self._persist_node_result,
                        execution_id,
                        node.id,
                        ExecutionStatus.COMPLETED,
                        result=result,
                        retry_count=attempt,
                        duration_ms=duration_ms,
                        queued_ms=queued_ms,
                        started_at=started_iso,
                        finished_at=_now_iso(),
                        iteration=iteration,
                        attempt_metrics={
                            "attempts": attempts,
                            "metrics": metrics.to_dict(),
                        },
                    )
                    execution_broker.publish(
                        execution_id,
                        stream_events.EVENT_NODE_FINISHED,
                        node_id=node.id,
                        node_name=node.name,
                        status=ExecutionStatus.COMPLETED.value,
                        duration_ms=round(duration_ms, 2),
                        metrics=metrics.to_dict(),
                        branches=branches,
                    )
                    event_bus.publish(
                        EVENT_NODE_FINISHED,
                        execution_id=execution_id,
                        node_id=node.id,
                        node_name=node.name,
                        status=ExecutionStatus.COMPLETED.value,
                        duration_ms=duration_ms,
                    )
                    return node.id, result, metrics, branches

                except asyncio.CancelledError:
                    await asyncio.to_thread(
                        self._persist_node_result,
                        execution_id,
                        node.id,
                        ExecutionStatus.CANCELLED,
                        finished_at=_now_iso(),
                    )
                    raise

                except asyncio.TimeoutError as exc:
                    last_error = f"Node timed out after {timeout}s"
                    last_code = NodeErrorCode.TIMEOUT
                except Exception as exc:  # noqa: BLE001 - executor errors are data
                    last_error = f"{type(exc).__name__}: {exc}"
                    last_code = classify_exception(exc)

                attempts.append(
                    {
                        "attempt": attempt + 1,
                        "ok": False,
                        "error": last_error[:500],
                        "code": last_code.value,
                        "duration_ms": round(
                            (time.perf_counter() - attempt_started) * 1000, 3
                        ),
                    }
                )
                execution_broker.log(
                    execution_id,
                    f"Node {node.name or node.id} attempt {attempt + 1}/{max_retries} "
                    f"failed ({last_code.value}): {last_error}",
                    level="WARNING",
                    node_id=node.id,
                )

                # A validation/permission error will never succeed on retry, so
                # fail immediately instead of burning the remaining attempts.
                is_last = attempt == max_retries - 1
                if is_last or not last_code.is_retryable:
                    duration_ms = (time.perf_counter() - started) * 1000
                    await asyncio.to_thread(
                        self._persist_node_result,
                        execution_id,
                        node.id,
                        ExecutionStatus.FAILED,
                        error=last_error,
                        error_code=last_code.value,
                        retry_count=attempt + 1,
                        duration_ms=duration_ms,
                        queued_ms=queued_ms,
                        started_at=started_iso,
                        finished_at=_now_iso(),
                        iteration=iteration,
                        attempt_metrics={"attempts": attempts},
                    )
                    execution_broker.publish(
                        execution_id,
                        stream_events.EVENT_NODE_FINISHED,
                        node_id=node.id,
                        node_name=node.name,
                        status=ExecutionStatus.FAILED.value,
                        error=last_error,
                        error_code=last_code.value,
                    )
                    event_bus.publish(
                        EVENT_NODE_FINISHED,
                        execution_id=execution_id,
                        node_id=node.id,
                        node_name=node.name,
                        status=ExecutionStatus.FAILED.value,
                        error=last_error,
                    )
                    raise NodeFailure(node.id, node.name, last_error)

                execution_broker.publish(
                    execution_id,
                    stream_events.EVENT_NODE_RETRY,
                    node_id=node.id,
                    attempt=attempt + 1,
                    error=last_error,
                    error_code=last_code.value,
                )
                event_bus.publish(
                    EVENT_NODE_RETRY,
                    execution_id=execution_id,
                    node_id=node.id,
                    attempt=attempt + 1,
                    error=last_error,
                )
                await asyncio.sleep(base_delay * (2 ** attempt))

            raise NodeFailure(node.id, node.name, last_error)


class _NodeSnapshot:
    """Detached, session-independent view of a ``Node`` row.

    Executors only ever need these fields, and detaching avoids
    ``DetachedInstanceError`` plus keeps the DB session lifetime short.
    """

    __slots__ = (
        "id", "name", "node_type", "config", "input_schema",
        "output_schema", "retry_policy", "workflow_id",
    )

    def __init__(
        self,
        id: int,
        name: str,
        node_type: str,
        config: Any = None,
        input_schema: Any = None,
        output_schema: Any = None,
        retry_policy: Any = None,
        workflow_id: Optional[int] = None,
    ) -> None:
        self.id = id
        self.name = name
        self.node_type = node_type
        self.config = config
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.retry_policy = retry_policy
        self.workflow_id = workflow_id

    @classmethod
    def from_orm_node(cls, node) -> "_NodeSnapshot":
        return cls(
            id=node.id,
            name=node.name,
            node_type=node.node_type,
            config=node.config,
            input_schema=node.input_schema,
            output_schema=node.output_schema,
            retry_policy=getattr(node, "retry_policy", None),
            workflow_id=node.workflow_id,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Node {self.id} {self.name!r} type={self.node_type!r}>"


workflow_engine = WorkflowEngine()
