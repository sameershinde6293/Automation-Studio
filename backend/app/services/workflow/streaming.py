"""Real-time execution event broker and durable log buffer.

Two problems M4 has to solve:

1. **Live updates.** The editor needs node-by-node progress without polling.
   The M1 event bus is in-process and fan-out only; it has no per-execution
   subscription and its 200-entry global ring buffer evicts execution events
   almost immediately.
2. **Durable logs.** Log lines must survive the run so the log viewer can page
   through them and a reconnecting client can replay what it missed.

:class:`ExecutionBroker` handles both. Subscribers get a **bounded** queue: a
slow SSE client is dropped rather than being allowed to stall the engine
(back-pressure gap P8 in the M4 audit). Log rows are batched before hitting the
database (bottleneck B5) and mirrored into a per-execution ring buffer so a new
subscriber can be backfilled without a query.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Set

from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger

logger = get_logger("workflow.streaming")

# Event names published for live consumers. The engine also mirrors the M1
# ``workflow.*`` events onto the global event bus for backwards compatibility.
EVENT_EXECUTION_QUEUED = "execution.queued"
EVENT_EXECUTION_STARTED = "execution.started"
EVENT_EXECUTION_PAUSED = "execution.paused"
EVENT_EXECUTION_RESUMED = "execution.resumed"
EVENT_EXECUTION_STOPPING = "execution.stopping"
EVENT_EXECUTION_FINISHED = "execution.finished"
EVENT_EXECUTION_PROGRESS = "execution.progress"
EVENT_NODE_STARTED = "node.started"
EVENT_NODE_FINISHED = "node.finished"
EVENT_NODE_RETRY = "node.retry"
EVENT_NODE_SKIPPED = "node.skipped"
EVENT_LOG = "log"
EVENT_HEARTBEAT = "heartbeat"


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExecutionEvent:
    """One live event for a single execution."""

    execution_id: int
    event: str
    sequence: int
    at: str = field(default_factory=utc_iso)
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "event": self.event,
            "sequence": self.sequence,
            "at": self.at,
            **self.payload,
        }


class _Subscription:
    """A bounded, drop-oldest queue for one live subscriber."""

    __slots__ = ("queue", "execution_id", "dropped", "created_at")

    def __init__(self, execution_id: Optional[int], maxsize: int) -> None:
        self.execution_id = execution_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.created_at = time.time()

    def offer(self, event: ExecutionEvent) -> None:
        """Non-blocking put. Drops the oldest event when the client is behind."""
        try:
            self.queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        # Client is not keeping up: discard the oldest event to make room so the
        # newest state still gets through. The engine is never blocked.
        try:
            self.queue.get_nowait()
            self.dropped += 1
        except asyncio.QueueEmpty:  # pragma: no cover - race
            pass
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - race
            self.dropped += 1


class ExecutionBroker:
    """Fan-out of execution events plus a batched, durable log writer.

    Thread-safety: the engine writes node status from worker threads via
    ``asyncio.to_thread``, so ``publish`` and ``log`` are guarded by a lock and
    deliver to subscriber queues using ``call_soon_threadsafe`` when they are
    invoked from outside the loop.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs_by_execution: Dict[int, Set[_Subscription]] = defaultdict(set)
        self._global_subs: Set[_Subscription] = set()
        self._sequences: Dict[int, int] = defaultdict(int)
        self._event_buffers: Dict[int, Deque[ExecutionEvent]] = {}
        self._log_buffers: Dict[int, Deque[Dict[str, Any]]] = {}
        self._pending_logs: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        self._last_flush: Dict[int, float] = defaultdict(float)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        #: Injected in tests to avoid touching the database.
        self._persist_enabled = True

    # ------------------------------------------------------------------ #
    # Loop binding
    # ------------------------------------------------------------------ #
    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Remember the event loop so worker threads can deliver events."""
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    # ------------------------------------------------------------------ #
    # Subscriptions
    # ------------------------------------------------------------------ #
    def subscribe(
        self, execution_id: Optional[int] = None, *, maxsize: Optional[int] = None
    ) -> _Subscription:
        """Subscribe to one execution, or to all when ``execution_id`` is None."""
        self.bind_loop()
        size = maxsize or max(8, settings.EXECUTION_STREAM_QUEUE_SIZE)
        subscription = _Subscription(execution_id, size)
        with self._lock:
            if execution_id is None:
                self._global_subs.add(subscription)
            else:
                self._subs_by_execution[execution_id].add(subscription)
        return subscription

    def unsubscribe(self, subscription: _Subscription) -> None:
        with self._lock:
            if subscription.execution_id is None:
                self._global_subs.discard(subscription)
            else:
                subs = self._subs_by_execution.get(subscription.execution_id)
                if subs:
                    subs.discard(subscription)
                    if not subs:
                        self._subs_by_execution.pop(subscription.execution_id, None)

    def subscriber_count(self, execution_id: Optional[int] = None) -> int:
        with self._lock:
            if execution_id is None:
                return len(self._global_subs) + sum(
                    len(s) for s in self._subs_by_execution.values()
                )
            return len(self._subs_by_execution.get(execution_id, ()))

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #
    def next_sequence(self, execution_id: int) -> int:
        with self._lock:
            self._sequences[execution_id] += 1
            return self._sequences[execution_id]

    def publish(self, execution_id: int, event: str, **payload: Any) -> ExecutionEvent:
        """Publish a live event. Never blocks and never raises."""
        record = ExecutionEvent(
            execution_id=execution_id,
            event=event,
            sequence=self.next_sequence(execution_id),
            payload=payload,
        )
        with self._lock:
            buffer = self._event_buffers.get(execution_id)
            if buffer is None:
                buffer = deque(maxlen=max(16, settings.EXECUTION_LOG_BUFFER_SIZE))
                self._event_buffers[execution_id] = buffer
            buffer.append(record)
            targets = list(self._subs_by_execution.get(execution_id, ())) + list(
                self._global_subs
            )

        if not targets:
            return record

        loop = self._loop
        running: Optional[asyncio.AbstractEventLoop]
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is not None:
            for subscription in targets:
                subscription.offer(record)
        elif loop is not None and not loop.is_closed():
            # Called from a worker thread: hop onto the loop thread to touch
            # asyncio.Queue safely.
            for subscription in targets:
                try:
                    loop.call_soon_threadsafe(subscription.offer, record)
                except RuntimeError:  # pragma: no cover - loop shutting down
                    pass
        else:
            for subscription in targets:
                subscription.offer(record)
        return record

    def replay_events(
        self, execution_id: int, after_sequence: int = 0
    ) -> List[ExecutionEvent]:
        """Buffered events newer than ``after_sequence`` (for SSE reconnects)."""
        with self._lock:
            buffer = self._event_buffers.get(execution_id)
            if not buffer:
                return []
            return [e for e in buffer if e.sequence > after_sequence]

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    def log(
        self,
        execution_id: int,
        message: str,
        *,
        level: str = "INFO",
        node_id: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None,
        flush: bool = False,
    ) -> Dict[str, Any]:
        """Record a log line: buffered in memory, batched to the database and
        published live."""
        level = str(level).upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            level = "INFO"
        record = {
            "execution_id": execution_id,
            "message": str(message)[:8000],
            "level": level,
            "node_id": node_id,
            "context": context or None,
            "at": utc_iso(),
        }

        with self._lock:
            buffer = self._log_buffers.get(execution_id)
            if buffer is None:
                buffer = deque(maxlen=max(16, settings.EXECUTION_LOG_BUFFER_SIZE))
                self._log_buffers[execution_id] = buffer
            record["sequence"] = len(buffer) + 1
            buffer.append(record)
            self._pending_logs[execution_id].append(dict(record))
            pending_count = len(self._pending_logs[execution_id])
            last = self._last_flush.get(execution_id, 0.0)

        due = (
            flush
            or level == "ERROR"
            or pending_count >= max(1, settings.EXECUTION_LOG_BATCH_SIZE)
            or (time.time() - last) >= settings.EXECUTION_LOG_FLUSH_INTERVAL
        )
        if due:
            self.flush_logs(execution_id)

        self.publish(
            execution_id,
            EVENT_LOG,
            level=level,
            message=record["message"],
            node_id=node_id,
            log_sequence=record["sequence"],
            context=context,
        )
        return record

    def flush_logs(self, execution_id: int) -> int:
        """Write buffered log rows in a single transaction. Returns row count."""
        with self._lock:
            pending = self._pending_logs.pop(execution_id, [])
            self._last_flush[execution_id] = time.time()
        if not pending or not self._persist_enabled:
            return 0

        try:
            from app.domain.repositories.workflow_repository import (
                ExecutionLogCreate,
                execution_log_repo,
            )
            from app.infrastructure.database.database import SessionLocal

            with SessionLocal() as db:
                base = execution_log_repo.next_sequence(db, execution_id)
                records = []
                for offset, item in enumerate(pending):
                    records.append(
                        ExecutionLogCreate(
                            execution_id=execution_id,
                            message=item["message"],
                            level=item["level"],
                            node_id=item.get("node_id"),
                            sequence=base + offset,
                            context=item.get("context"),
                        )
                    )
                execution_log_repo.bulk_append(db, records)
            return len(records)
        except Exception:
            # Logging must never break an execution.
            logger.exception("Failed to flush %s execution log row(s)", len(pending))
            return 0

    def recent_logs(
        self, execution_id: int, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """In-memory log tail (no database round trip)."""
        with self._lock:
            buffer = self._log_buffers.get(execution_id)
            if not buffer:
                return []
            return list(buffer)[-limit:]

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def finish(self, execution_id: int) -> None:
        """Flush anything outstanding for a finished execution."""
        self.flush_logs(execution_id)

    def cleanup(self, execution_id: int) -> None:
        """Drop in-memory buffers for an execution (after the last subscriber)."""
        self.flush_logs(execution_id)
        with self._lock:
            if self._subs_by_execution.get(execution_id):
                return
            self._event_buffers.pop(execution_id, None)
            self._log_buffers.pop(execution_id, None)
            self._sequences.pop(execution_id, None)
            self._last_flush.pop(execution_id, None)

    def reset(self) -> None:
        """Clear all state. Used by tests."""
        with self._lock:
            self._subs_by_execution.clear()
            self._global_subs.clear()
            self._sequences.clear()
            self._event_buffers.clear()
            self._log_buffers.clear()
            self._pending_logs.clear()
            self._last_flush.clear()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "subscribers": len(self._global_subs)
                + sum(len(s) for s in self._subs_by_execution.values()),
                "tracked_executions": len(self._event_buffers),
                "pending_log_rows": sum(len(v) for v in self._pending_logs.values()),
                "dropped_events": sum(
                    s.dropped
                    for subs in self._subs_by_execution.values()
                    for s in subs
                )
                + sum(s.dropped for s in self._global_subs),
            }


execution_broker = ExecutionBroker()


def format_sse(event: ExecutionEvent) -> str:
    """Render an event as a Server-Sent Events frame."""
    import json

    data = json.dumps(event.to_dict(), default=str)
    return f"id: {event.sequence}\nevent: {event.event}\ndata: {data}\n\n"
