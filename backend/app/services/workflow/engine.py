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
from app.domain.models.workflow import ExecutionStatus, NodeExecution
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

from .graph import build_adjacency, descendants, validate_graph

logger = get_logger("workflow")

# Event names published on the event bus.
EVENT_EXECUTION_STARTED = "workflow.execution.started"
EVENT_EXECUTION_FINISHED = "workflow.execution.finished"
EVENT_NODE_STARTED = "workflow.node.started"
EVENT_NODE_FINISHED = "workflow.node.finished"
EVENT_NODE_RETRY = "workflow.node.retry"

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
    def __init__(self) -> None:
        self.active_tasks: Dict[int, asyncio.Task] = {}
        # Node status writes happen from a thread pool while many nodes run in
        # parallel. SQLite (and the get-or-create pattern below) is not safe
        # under concurrent inserts for the same row, so writes are serialised.
        # The lock is only held for the duration of a short DB transaction.
        self._write_lock = threading.Lock()

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
        """Request cancellation. Returns True if a live task was cancelled."""
        task = self.active_tasks.get(execution_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

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
