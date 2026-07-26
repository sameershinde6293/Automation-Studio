"""Shared API response schemas."""

from __future__ import annotations

from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Paginated collection envelope."""

    items: List[T]
    total: int = Field(..., description="Total rows matching the query")
    skip: int = 0
    limit: int = 100

    @property
    def has_more(self) -> bool:
        return self.skip + len(self.items) < self.total


class Message(BaseModel):
    """Simple acknowledgement payload."""

    status: str = "ok"
    message: str = ""
    details: Optional[Dict[str, Any]] = None
