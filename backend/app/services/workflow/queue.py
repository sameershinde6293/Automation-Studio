"""Bounded priority queue and worker pool for workflow executions.

Before M4 ``WorkflowEngine.submit()`` called ``asyncio.create_task`` directly:
there was no admission control, no ordering and no back-pressure, so N
simultaneous API calls produced N concurrent executions (gap R5 / bottleneck B6).

This module introduces:

* a **priority queue** ordered by ``(priority, sequence)`` — lower priority
  value first, FIFO within a priority band;
* a **worker pool** of ``EXECUTION_MAX_WORKERS`` coroutines pulling from it;
* **admission control** — the queue is capped at
  ``EXECUTION_QUEUE_MAX_SIZE`` and rejects beyond that so callers get a clear
  429 instead of the process degrading;
* **cancellation of queued items** — cancelling a run that has not started yet
  removes it from the queue without ever executing it.

The pool is lazily started on first submit so importing the module (and the
825 existing tests) costs nothing.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from app.core.errors import CreatorOSError
from app.domain.models.workflow import ExecutionPriority
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger

logger = get_logger("workflow.queue")


class QueueFullError(CreatorOSError):
    """Raised when the execution queue is at capacity."""

    status_code = 429
    code = "queue_full"


@dataclass(order=True)
class QueuedExecution:
    """One entry in the priority queue."""

    priority: int
    sequence: int
    execution_id: int = field(compare=False)
    enqueued_at: float = field(default_factory=time.monotonic, compare=False)
    metadata: Dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def wait_seconds(self) -> float:
        return round(time.monotonic() - self.enqueued_at, 3)


class ExecutionQueue:
    """Async priority queue with capacity limits and cancellation support."""

    def __init__(self) -> None:
        self._heap: List[QueuedExecution] = []
        self._counter = itertools.count()
        self._lock = threading.RLock()
        self._cancelled: Set[int] = set()
        self._queued_ids: Set[int] = set()
        self._not_empty: Optional[asyncio.Event] = None

    def _ensure_event(self) -> asyncio.Event:
        if self._not_empty is None:
            self._not_empty = asyncio.Event()
        return self._not_empty

    # ------------------------------------------------------------------ #
    # Producer side
    # ------------------------------------------------------------------ #
    def put(
        self,
        execution_id: int,
        priority: int = ExecutionPriority.NORMAL.value,
        **metadata: Any,
    ) -> QueuedExecution:
        """Enqueue an execution. Raises :class:`QueueFullError` at capacity."""
        maximum = max(1, settings.EXECUTION_QUEUE_MAX_SIZE)
        with self._lock:
            if len(self._heap) >= maximum:
                raise QueueFullError(
                    f"Execution queue is full ({maximum} waiting).",
                    details={"queue_size": len(self._heap), "max_size": maximum},
                )
            if execution_id in self._queued_ids:
                # Idempotent: never queue the same execution twice.
                for item in self._heap:
                    if item.execution_id == execution_id:
                        return item
            item = QueuedExecution(
                priority=int(priority),
                sequence=next(self._counter),
                execution_id=execution_id,
                metadata=metadata,
            )
            heapq.heappush(self._heap, item)
            self._queued_ids.add(execution_id)
            self._cancelled.discard(execution_id)
            event = self._not_empty
        if event is not None:
            self._set_event(event)
        return item

    def _set_event(self, event: asyncio.Event) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            event.set()
            return
        # Called off-loop: best effort.
        try:
            event.set()
        except Exception:  # pragma: no cover - defensive
            pass

    # ------------------------------------------------------------------ #
    # Consumer side
    # ------------------------------------------------------------------ #
    def _pop_ready(self) -> Optional[QueuedExecution]:
        with self._lock:
            while self._heap:
                item = heapq.heappop(self._heap)
                self._queued_ids.discard(item.execution_id)
                if item.execution_id in self._cancelled:
                    self._cancelled.discard(item.execution_id)
                    logger.info(
                        "Execution %s was cancelled while queued; skipping.",
                        item.execution_id,
                    )
                    continue
                return item
            if self._not_empty is not None:
                self._not_empty.clear()
            return None

    async def get(self, timeout: Optional[float] = None) -> Optional[QueuedExecution]:
        """Await the next runnable entry. ``None`` when the timeout expires."""
        event = self._ensure_event()
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            item = self._pop_ready()
            if item is not None:
                return item
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    def get_nowait(self) -> Optional[QueuedExecution]:
        return self._pop_ready()

    # ------------------------------------------------------------------ #
    # Management
    # ------------------------------------------------------------------ #
    def cancel(self, execution_id: int) -> bool:
        """Mark a queued execution as cancelled. True if it was waiting."""
        with self._lock:
            if execution_id in self._queued_ids:
                self._cancelled.add(execution_id)
                self._heap = [
                    item for item in self._heap if item.execution_id != execution_id
                ]
                heapq.heapify(self._heap)
                self._queued_ids.discard(execution_id)
                self._cancelled.discard(execution_id)
                return True
            return False

    def contains(self, execution_id: int) -> bool:
        with self._lock:
            return execution_id in self._queued_ids

    def position(self, execution_id: int) -> Optional[int]:
        """1-based position in dequeue order, or None when not queued."""
        with self._lock:
            ordered = sorted(self._heap)
        for index, item in enumerate(ordered, start=1):
            if item.execution_id == execution_id:
                return index
        return None

    def size(self) -> int:
        with self._lock:
            return len(self._heap)

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            ordered = sorted(self._heap)
        return [
            {
                "execution_id": item.execution_id,
                "priority": item.priority,
                "position": index,
                "waiting_seconds": item.wait_seconds,
                **item.metadata,
            }
            for index, item in enumerate(ordered, start=1)
        ]

    def clear(self) -> None:
        with self._lock:
            self._heap.clear()
            self._queued_ids.clear()
            self._cancelled.clear()
            if self._not_empty is not None:
                self._not_empty.clear()


class WorkerPool:
    """Fixed-size pool of coroutines consuming :class:`ExecutionQueue`."""

    def __init__(
        self,
        queue: ExecutionQueue,
        handler: Callable[[int], Awaitable[Any]],
        *,
        size: Optional[int] = None,
    ) -> None:
        self.queue = queue
        self.handler = handler
        self._size = size
        self._workers: List[asyncio.Task] = []
        self._running = False
        self._active: Dict[int, float] = {}
        self._lock = threading.RLock()
        self._completed = 0
        self._failed = 0

    @property
    def size(self) -> int:
        return self._size or max(1, settings.EXECUTION_MAX_WORKERS)

    @property
    def is_running(self) -> bool:
        return self._running and any(not w.done() for w in self._workers)

    def start(self) -> None:
        """Start workers on the current loop. Idempotent."""
        if self.is_running:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("WorkerPool.start() called without a running loop.")
            return
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"execution-worker-{index}")
            for index in range(self.size)
        ]
        self._running = True
        logger.info("Execution worker pool started with %s worker(s).", self.size)

    async def _worker(self, index: int) -> None:
        while True:
            try:
                item = await self.queue.get(timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("Worker %s failed to dequeue.", index)
                await asyncio.sleep(0.5)
                continue
            if item is None:
                continue

            with self._lock:
                self._active[item.execution_id] = time.monotonic()
            try:
                await self.handler(item.execution_id)
                with self._lock:
                    self._completed += 1
            except asyncio.CancelledError:
                with self._lock:
                    self._active.pop(item.execution_id, None)
                raise
            except Exception:
                with self._lock:
                    self._failed += 1
                logger.exception(
                    "Worker %s: execution %s raised.", index, item.execution_id
                )
            finally:
                with self._lock:
                    self._active.pop(item.execution_id, None)

    def active_executions(self) -> List[int]:
        with self._lock:
            return sorted(self._active)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "workers": self.size,
                "running": self.is_running,
                "active": len(self._active),
                "active_executions": sorted(self._active),
                "completed": self._completed,
                "failed": self._failed,
            }

    async def shutdown(self, timeout: float = 5.0) -> None:
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.wait(self._workers, timeout=timeout)
        self._workers.clear()
        self._running = False
        with self._lock:
            self._active.clear()


execution_queue = ExecutionQueue()
