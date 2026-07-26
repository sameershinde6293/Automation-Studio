"""In-process publish/subscribe event bus.

Backwards compatible with V1.0 (``subscribe``, ``publish``, ``event_bus``).

V1.1 improvements:
- a failing subscriber no longer aborts the publish (errors are isolated+logged)
- async subscribers are supported (``publish_async`` awaits them; ``publish``
  schedules them on the running loop)
- ``unsubscribe`` / ``clear`` for deterministic tests and plugin teardown
- wildcard (``*``) subscribers for logging/telemetry sinks
- bounded ring buffer of recent events, used by the UI activity feed
"""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

from app.infrastructure.logging.logger import get_logger

logger = get_logger("events")

WILDCARD = "*"


class EventBus:
    def __init__(self, history_size: int = 200) -> None:
        self._subscribers: Dict[str, List[Callable]] = {}
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history_size)

    # -- subscription ----------------------------------------------------- #
    def subscribe(self, event_type: str, callback: Callable) -> Callable:
        """Register ``callback`` for ``event_type``. Returns the callback so it
        can be used as a decorator."""
        if not callable(callback):
            raise TypeError("Event subscriber must be callable.")
        self._subscribers.setdefault(event_type, []).append(callback)
        return callback

    def unsubscribe(self, event_type: str, callback: Callable) -> bool:
        handlers = self._subscribers.get(event_type)
        if not handlers:
            return False
        try:
            handlers.remove(callback)
        except ValueError:
            return False
        if not handlers:
            self._subscribers.pop(event_type, None)
        return True

    def clear(self, event_type: Optional[str] = None) -> None:
        if event_type is None:
            self._subscribers.clear()
        else:
            self._subscribers.pop(event_type, None)

    def subscriber_count(self, event_type: str) -> int:
        return len(self._subscribers.get(event_type, []))

    # -- publishing ------------------------------------------------------- #
    def _handlers_for(self, event_type: str) -> List[Callable]:
        return list(self._subscribers.get(event_type, [])) + list(
            self._subscribers.get(WILDCARD, [])
        )

    def _record(self, event_type: str, payload: Dict[str, Any]) -> None:
        self._history.append(
            {
                "event": event_type,
                "at": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }
        )

    def publish(self, event_type: str, **kwargs: Any) -> List[Any]:
        """Publish synchronously.

        Sync subscribers run inline. Async subscribers are scheduled on the
        running event loop when one exists, otherwise they are executed to
        completion via ``asyncio.run``.
        """
        self._record(event_type, kwargs)
        results: List[Any] = []
        for callback in self._handlers_for(event_type):
            try:
                if inspect.iscoroutinefunction(callback):
                    self._dispatch_async(callback, event_type, kwargs)
                    continue
                payload = self._invoke(callback, event_type, kwargs)
                if inspect.isawaitable(payload):
                    self._schedule_awaitable(payload)
                else:
                    results.append(payload)
            except Exception:
                logger.exception(
                    "Event subscriber failed for %r (subscriber=%s)",
                    event_type,
                    getattr(callback, "__name__", repr(callback)),
                )
        return results

    async def publish_async(self, event_type: str, **kwargs: Any) -> List[Any]:
        """Publish and await every async subscriber."""
        self._record(event_type, kwargs)
        results: List[Any] = []
        for callback in self._handlers_for(event_type):
            try:
                payload = self._invoke(callback, event_type, kwargs)
                if inspect.isawaitable(payload):
                    payload = await payload
                results.append(payload)
            except Exception:
                logger.exception(
                    "Async event subscriber failed for %r (subscriber=%s)",
                    event_type,
                    getattr(callback, "__name__", repr(callback)),
                )
        return results

    @staticmethod
    def _invoke(callback: Callable, event_type: str, kwargs: Dict[str, Any]) -> Any:
        """Call a subscriber, passing ``event_type`` only if it accepts it."""
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):  # builtins / C callables
            return callback(**kwargs)
        if "event_type" in signature.parameters and "event_type" not in kwargs:
            return callback(event_type=event_type, **kwargs)
        return callback(**kwargs)

    def _dispatch_async(
        self, callback: Callable, event_type: str, kwargs: Dict[str, Any]
    ) -> None:
        coro = self._invoke(callback, event_type, kwargs)
        self._schedule_awaitable(coro)

    @staticmethod
    def _schedule_awaitable(awaitable: Any) -> None:
        async def _guarded() -> None:
            try:
                await awaitable
            except Exception:
                logger.exception("Async event subscriber raised.")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(_guarded())
            except Exception:
                logger.exception("Could not run async event subscriber.")
            return
        loop.create_task(_guarded())

    # -- history ---------------------------------------------------------- #
    def recent(self, limit: int = 50, event_type: Optional[str] = None) -> List[Dict[str, Any]]:
        items = list(self._history)
        if event_type:
            items = [e for e in items if e["event"] == event_type]
        return items[-limit:][::-1]


event_bus = EventBus()
