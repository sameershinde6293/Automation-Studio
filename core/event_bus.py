"""Thread-safe publish/subscribe event bus for Autopilot.

Modules and UI components communicate cross-layer only through this bus
or return values — never via direct module-to-module imports.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable, DefaultDict, List, Optional

logger = logging.getLogger("autopilot.event_bus")

EventCallback = Callable[[Any], None]


class EventBus:
    """In-process event bus with subscribe/publish/unsubscribe."""

    def __init__(self) -> None:
        """Initialize empty subscriber registry."""
        self._subscribers: DefaultDict[str, List[EventCallback]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, event_name: str, callback: EventCallback) -> None:
        """Register a callback for an event.

        Args:
            event_name: Event identifier (e.g. 'render_state_changed').
            callback: Callable receiving event data.
        """
        with self._lock:
            if callback not in self._subscribers[event_name]:
                self._subscribers[event_name].append(callback)
                logger.debug("Subscribed to event: %s", event_name)

    def unsubscribe(self, event_name: str, callback: EventCallback) -> None:
        """Remove a callback for an event.

        Args:
            event_name: Event identifier.
            callback: Previously registered callback.
        """
        with self._lock:
            callbacks = self._subscribers.get(event_name, [])
            if callback in callbacks:
                callbacks.remove(callback)
                logger.debug("Unsubscribed from event: %s", event_name)

    def publish(self, event_name: str, data: Any = None) -> int:
        """Publish an event to all subscribers.

        Callbacks run synchronously. Exceptions in callbacks are logged
        and do not stop other subscribers.

        Args:
            event_name: Event identifier.
            data: Payload passed to each callback.

        Returns:
            Number of callbacks invoked.
        """
        with self._lock:
            callbacks = list(self._subscribers.get(event_name, []))

        invoked = 0
        for callback in callbacks:
            try:
                callback(data)
                invoked += 1
            except Exception as exc:  # noqa: BLE001 - isolate subscriber faults
                logger.error(
                    "Event callback error for '%s': %s",
                    event_name,
                    exc,
                    exc_info=True,
                )
        logger.debug("Published event '%s' to %s subscribers", event_name, invoked)
        return invoked

    def clear(self, event_name: Optional[str] = None) -> None:
        """Clear subscribers for one event or all events.

        Args:
            event_name: Specific event, or None to clear all.
        """
        with self._lock:
            if event_name is None:
                self._subscribers.clear()
            elif event_name in self._subscribers:
                del self._subscribers[event_name]

    def subscriber_count(self, event_name: str) -> int:
        """Return number of subscribers for an event.

        Args:
            event_name: Event identifier.

        Returns:
            Subscriber count.
        """
        with self._lock:
            return len(self._subscribers.get(event_name, []))

    def list_events(self) -> List[str]:
        """Return event names that currently have subscribers.

        Returns:
            Sorted list of event names.
        """
        with self._lock:
            return sorted(self._subscribers.keys())
