"""Execution control signals: pause, resume, stop and cancel.

The M1 engine only supported hard cancellation via ``asyncio.Task.cancel()``.
``ExecutionStatus.PAUSED`` existed in the enum but nothing ever set or honoured
it (gap R1). This module supplies the missing primitives.

Semantics
---------
``pause``   Stop scheduling *new* nodes. Nodes already running are allowed to
            finish. The execution parks until resumed, stopped or cancelled.
``resume``  Leave the paused state and continue scheduling.
``stop``    Graceful: stop scheduling new nodes, let in-flight nodes finish,
            then terminate with status ``CANCELLED``.
``cancel``  Hard: cancel the asyncio task immediately; in-flight nodes get a
            ``CancelledError``.

A :class:`ControlHandle` is created per execution and owned by the engine. It
is deliberately loop-affine — the engine awaits its events — but the *requests*
(``request_pause`` etc.) are safe to call from any thread, which matters because
the API layer runs in FastAPI's threadpool.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.infrastructure.logging.logger import get_logger

logger = get_logger("workflow.control")


class ControlSignal:
    """String constants for the control requests."""

    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    CANCEL = "cancel"


@dataclass
class ControlHandle:
    """Per-execution pause/stop state.

    ``asyncio.Event`` is not thread-safe, so mutations go through a
    ``threading.Lock`` and only plain boolean flags are read from the engine's
    hot path. The events are used purely to wake the scheduler promptly.
    """

    execution_id: int
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _paused: bool = False
    _stopping: bool = False
    _cancelled: bool = False
    _pause_requested_at: Optional[float] = None
    _total_paused_seconds: float = 0.0
    #: Set while running, cleared while paused. Awaited by the scheduler.
    _resume_event: Optional[asyncio.Event] = field(default=None, repr=False)
    #: Set when a stop/cancel is requested; long-running nodes may poll it.
    _stop_event: Optional[asyncio.Event] = field(default=None, repr=False)
    _loop: Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False)

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Create the asyncio events on the loop that will run the execution."""
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        if self._resume_event is None:
            self._resume_event = asyncio.Event()
            self._resume_event.set()
        if self._stop_event is None:
            self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def is_stopping(self) -> bool:
        with self._lock:
            return self._stopping

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def should_halt(self) -> bool:
        """True when no further nodes should be scheduled."""
        with self._lock:
            return self._stopping or self._cancelled

    @property
    def total_paused_seconds(self) -> float:
        with self._lock:
            extra = (
                time.monotonic() - self._pause_requested_at
                if self._paused and self._pause_requested_at
                else 0.0
            )
            return round(self._total_paused_seconds + extra, 3)

    # ------------------------------------------------------------------ #
    # Requests (thread-safe)
    # ------------------------------------------------------------------ #
    def _wake(self, event: Optional[asyncio.Event], set_it: bool) -> None:
        """Set/clear an asyncio.Event from any thread."""
        if event is None:
            return

        def _apply() -> None:
            if set_it:
                event.set()
            else:
                event.clear()

        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and (loop is None or running is loop):
            _apply()
        elif loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(_apply)
            except RuntimeError:  # pragma: no cover - loop shutting down
                _apply()
        else:
            _apply()

    def request_pause(self) -> bool:
        """Ask the execution to pause. False if already paused or halting."""
        with self._lock:
            if self._paused or self._stopping or self._cancelled:
                return False
            self._paused = True
            self._pause_requested_at = time.monotonic()
            resume_event = self._resume_event
        self._wake(resume_event, False)
        logger.info("Execution %s: pause requested", self.execution_id)
        return True

    def request_resume(self) -> bool:
        """Resume a paused execution. False if it was not paused."""
        with self._lock:
            if not self._paused:
                return False
            self._paused = False
            if self._pause_requested_at is not None:
                self._total_paused_seconds += time.monotonic() - self._pause_requested_at
                self._pause_requested_at = None
            resume_event = self._resume_event
        self._wake(resume_event, True)
        logger.info("Execution %s: resumed", self.execution_id)
        return True

    def request_stop(self) -> bool:
        """Graceful stop: drain in-flight nodes, schedule nothing new."""
        with self._lock:
            if self._stopping or self._cancelled:
                return False
            self._stopping = True
            was_paused = self._paused
            self._paused = False
            if was_paused and self._pause_requested_at is not None:
                self._total_paused_seconds += time.monotonic() - self._pause_requested_at
                self._pause_requested_at = None
            resume_event = self._resume_event
            stop_event = self._stop_event
        # Unblock the scheduler so it observes the stop flag immediately.
        self._wake(resume_event, True)
        self._wake(stop_event, True)
        logger.info("Execution %s: graceful stop requested", self.execution_id)
        return True

    def request_cancel(self) -> bool:
        """Hard cancel. Also unparks a paused execution so it can unwind."""
        with self._lock:
            if self._cancelled:
                return False
            self._cancelled = True
            self._stopping = True
            self._paused = False
            resume_event = self._resume_event
            stop_event = self._stop_event
        self._wake(resume_event, True)
        self._wake(stop_event, True)
        logger.info("Execution %s: cancel requested", self.execution_id)
        return True

    # ------------------------------------------------------------------ #
    # Scheduler-side waiting
    # ------------------------------------------------------------------ #
    async def wait_if_paused(self, poll_seconds: float = 0.25) -> None:
        """Block while paused. Returns as soon as resumed/stopped/cancelled."""
        if not self.is_paused:
            return
        event = self._resume_event
        if event is None:
            # No loop bound (shouldn't happen in the engine); degrade to polling.
            while self.is_paused:
                await asyncio.sleep(poll_seconds)
            return
        while self.is_paused:
            try:
                await asyncio.wait_for(event.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                continue

    @property
    def stop_event(self) -> Optional[asyncio.Event]:
        """Event set on stop/cancel; nodes may race their work against it."""
        return self._stop_event

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "execution_id": self.execution_id,
                "paused": self._paused,
                "stopping": self._stopping,
                "cancelled": self._cancelled,
                "paused_seconds": self.total_paused_seconds,
            }


class ControlRegistry:
    """Process-wide registry of live :class:`ControlHandle` objects."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handles: Dict[int, ControlHandle] = {}

    def create(self, execution_id: int) -> ControlHandle:
        with self._lock:
            handle = ControlHandle(execution_id=execution_id)
            self._handles[execution_id] = handle
            return handle

    def get(self, execution_id: int) -> Optional[ControlHandle]:
        with self._lock:
            return self._handles.get(execution_id)

    def get_or_create(self, execution_id: int) -> ControlHandle:
        with self._lock:
            handle = self._handles.get(execution_id)
            if handle is None:
                handle = ControlHandle(execution_id=execution_id)
                self._handles[execution_id] = handle
            return handle

    def release(self, execution_id: int) -> None:
        with self._lock:
            self._handles.pop(execution_id, None)

    def active_ids(self) -> list[int]:
        with self._lock:
            return sorted(self._handles)

    def clear(self) -> None:
        with self._lock:
            self._handles.clear()


control_registry = ControlRegistry()
