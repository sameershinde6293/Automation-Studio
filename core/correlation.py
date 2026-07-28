"""Correlation ID context for render-scoped logging.

Every render session gets a short correlation ID. Log lines during that
render include the ID so a single session can be filtered instantly.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional


class CorrelationContext:
    """Thread-agnostic holder for the active render correlation ID."""

    _current_id: Optional[str] = None

    @classmethod
    def set(cls, correlation_id: str) -> None:
        """Set the active correlation ID.

        Args:
            correlation_id: Short ID string (typically 8 hex chars).
        """
        cls._current_id = correlation_id

    @classmethod
    def clear(cls) -> None:
        """Clear the active correlation ID."""
        cls._current_id = None

    @classmethod
    def get(cls) -> str:
        """Return the active correlation ID or empty string.

        Returns:
            Active ID or ''.
        """
        return cls._current_id or ""

    @classmethod
    def format_prefix(cls) -> str:
        """Return log prefix including brackets, or empty if none.

        Returns:
            Prefix like '[A3F7K291] ' or ''.
        """
        cid = cls._current_id
        return f"[{cid}] " if cid else ""

    @classmethod
    def new_id(cls) -> str:
        """Generate, set, and return a new correlation ID.

        Returns:
            New uppercase 8-character correlation ID.
        """
        correlation_id = str(uuid.uuid4()).replace("-", "")[:8].upper()
        cls.set(correlation_id)
        return correlation_id


class CorrelationFilter(logging.Filter):
    """Logging filter that prefixes messages with the correlation ID."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Inject correlation_prefix onto the log record.

        Args:
            record: Log record being processed.

        Returns:
            Always True (never drops records).
        """
        record.correlation_prefix = CorrelationContext.format_prefix()  # type: ignore[attr-defined]
        return True
