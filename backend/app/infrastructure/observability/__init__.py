"""Observability: metrics registry and error aggregation (M5)."""

from app.infrastructure.observability.errors import error_aggregator  # noqa: F401
from app.infrastructure.observability.metrics import registry  # noqa: F401

__all__ = ["error_aggregator", "registry"]
