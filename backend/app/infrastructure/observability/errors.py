"""Error aggregation (M5).

Before M5 an exception was logged and then gone. Answering "what is failing
right now?" meant grepping a log file, which is impossible from the UI and
impractical in a container.

:class:`ErrorAggregator` groups errors by fingerprint (type + location) and
keeps a bounded rolling window with first/last seen timestamps, counts and one
recent sample per group. It is memory-bounded by construction and thread-safe.

This is intentionally *not* a replacement for Sentry: it is in-process, lost on
restart and has no cross-instance view. It exists so that a single deployment
can answer "top errors in the last hour" without extra infrastructure. That
limitation is documented in docs/SECURITY.md and KNOWN_ISSUES.md.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional


@dataclass
class ErrorGroup:
    """Aggregated occurrences of one distinct error."""

    fingerprint: str
    error_type: str
    message: str
    location: str = ""
    count: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_request_id: Optional[str] = None
    last_path: Optional[str] = None
    samples: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=3))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "error_type": self.error_type,
            "message": self.message,
            "location": self.location,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_request_id": self.last_request_id,
            "last_path": self.last_path,
            "samples": list(self.samples),
        }


class ErrorAggregator:
    """Bounded, thread-safe rolling error index."""

    def __init__(self, max_groups: int = 500) -> None:
        self.max_groups = max_groups
        self._groups: Dict[str, ErrorGroup] = {}
        self._lock = threading.Lock()
        self._total = 0

    @staticmethod
    def fingerprint(error_type: str, location: str, message: str) -> str:
        """Stable id for an error class.

        The message is included only up to its first 80 characters so that
        errors differing solely by an embedded id still group together.
        """
        seed = f"{error_type}|{location}|{message[:80]}"
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]

    def record(
        self,
        exc: BaseException,
        *,
        request_id: Optional[str] = None,
        path: Optional[str] = None,
        method: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> ErrorGroup:
        """Record one occurrence and return its group."""
        error_type = type(exc).__name__
        message = str(exc)[:500]

        location = ""
        tb = getattr(exc, "__traceback__", None)
        while tb is not None:
            frame = tb.tb_frame
            location = f"{frame.f_code.co_filename}:{tb.tb_lineno}"
            tb = tb.tb_next  # walk to the innermost frame

        key = self.fingerprint(error_type, location, message)
        now = time.time()
        sample = {
            "timestamp": now,
            "request_id": request_id,
            "path": path,
            "method": method,
            "message": message,
            **(context or {}),
        }

        with self._lock:
            self._total += 1
            group = self._groups.get(key)
            if group is None:
                if len(self._groups) >= self.max_groups:
                    # Evict the least recently seen group to stay bounded.
                    oldest = min(self._groups.values(), key=lambda g: g.last_seen)
                    self._groups.pop(oldest.fingerprint, None)
                group = ErrorGroup(
                    fingerprint=key,
                    error_type=error_type,
                    message=message,
                    location=location,
                    first_seen=now,
                )
                self._groups[key] = group
            group.count += 1
            group.last_seen = now
            group.last_request_id = request_id
            group.last_path = path
            group.samples.append(sample)
            return group

    def top(self, limit: int = 20, since_seconds: Optional[float] = None) -> List[Dict[str, Any]]:
        """Most frequent error groups, newest activity first on ties."""
        cutoff = time.time() - since_seconds if since_seconds else None
        with self._lock:
            groups = [
                g for g in self._groups.values()
                if cutoff is None or g.last_seen >= cutoff
            ]
        groups.sort(key=lambda g: (g.count, g.last_seen), reverse=True)
        return [g.as_dict() for g in groups[: max(1, limit)]]

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            groups = list(self._groups.values())
            total = self._total
        return {
            "total_errors": total,
            "distinct_errors": len(groups),
            "last_error_at": max((g.last_seen for g in groups), default=None),
        }

    def clear(self) -> None:
        with self._lock:
            self._groups.clear()
            self._total = 0


error_aggregator = ErrorAggregator()
