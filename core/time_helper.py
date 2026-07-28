"""UTC time helpers for Autopilot.

All timestamps stored in the database and logs must use UTC.
This module centralizes time formatting so a future switch to
timezone-aware datetimes is a one-line change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


def utc_now() -> datetime:
    """Return current UTC datetime (naive, matching utcnow contract).

    Returns:
        datetime: Current UTC time without tzinfo (spec-compliant).
    """
    # Spec mandate: use datetime.utcnow(). Future: datetime.now(timezone.utc).
    return datetime.utcnow()


def utc_now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Return current UTC time as a formatted string.

    Args:
        fmt: strftime format string.

    Returns:
        Formatted UTC timestamp string.
    """
    return utc_now().strftime(fmt)


def parse_utc(value: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> Optional[datetime]:
    """Parse a UTC timestamp string.

    Args:
        value: Timestamp string.
        fmt: Expected format.

    Returns:
        Parsed datetime or None if parsing fails.
    """
    try:
        return datetime.strptime(value, fmt)
    except (TypeError, ValueError):
        return None
