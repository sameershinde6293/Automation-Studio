"""In-process metrics registry with Prometheus text exposition (M5).

``prometheus_client`` is deliberately not a dependency: Creator OS ships as a
local-first desktop application and every added runtime dependency is an
install failure waiting to happen. The subset actually needed — counters,
gauges and histograms with labels, rendered in the Prometheus text format — is
small, and implementing it here keeps ``/metrics`` scrapeable by any standard
agent without changing the install story.

Everything is thread-safe: metrics are updated from request handlers, engine
worker tasks and background threads simultaneously.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: Latency buckets in seconds, tuned for an API whose p50 is single-digit ms.
DEFAULT_BUCKETS: Tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

LabelValues = Tuple[str, ...]


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(names: Sequence[str], values: LabelValues) -> str:
    if not names:
        return ""
    pairs = ",".join(
        f'{name}="{_escape_label_value(str(value))}"'
        for name, value in zip(names, values)
    )
    return "{" + pairs + "}"


def _format_number(value: float) -> str:
    if value == math.inf:
        return "+Inf"
    if value == -math.inf:
        return "-Inf"
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return repr(float(value))


class _Metric:
    """Base class holding name, help text and label schema."""

    metric_type = "untyped"

    def __init__(
        self, name: str, documentation: str, labelnames: Sequence[str] = ()
    ) -> None:
        self.name = name
        self.documentation = documentation
        self.labelnames = tuple(labelnames)
        self._lock = threading.Lock()

    def _key(self, labels: Optional[Dict[str, str]]) -> LabelValues:
        if not self.labelnames:
            return ()
        provided = labels or {}
        missing = set(self.labelnames) - set(provided)
        if missing:
            raise ValueError(
                f"Metric {self.name} requires labels {sorted(self.labelnames)}; "
                f"missing {sorted(missing)}."
            )
        return tuple(str(provided[name]) for name in self.labelnames)

    def _header(self) -> List[str]:
        return [
            f"# HELP {self.name} {self.documentation}",
            f"# TYPE {self.name} {self.metric_type}",
        ]

    def render(self) -> List[str]:  # pragma: no cover - overridden
        raise NotImplementedError


class Counter(_Metric):
    """A monotonically increasing value."""

    metric_type = "counter"

    def __init__(self, name, documentation, labelnames=()) -> None:
        super().__init__(name, documentation, labelnames)
        self._values: Dict[LabelValues, float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError("Counters may not decrease.")
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def render(self) -> List[str]:
        with self._lock:
            snapshot = dict(self._values)
        if not snapshot:
            return []
        lines = self._header()
        for key, value in sorted(snapshot.items()):
            lines.append(
                f"{self.name}{_format_labels(self.labelnames, key)} "
                f"{_format_number(value)}"
            )
        return lines


class Gauge(_Metric):
    """A value that can go up and down."""

    metric_type = "gauge"

    def __init__(self, name, documentation, labelnames=()) -> None:
        super().__init__(name, documentation, labelnames)
        self._values: Dict[LabelValues, float] = {}

    def set(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def value(self, **labels: str) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def render(self) -> List[str]:
        with self._lock:
            snapshot = dict(self._values)
        if not snapshot:
            return []
        lines = self._header()
        for key, value in sorted(snapshot.items()):
            lines.append(
                f"{self.name}{_format_labels(self.labelnames, key)} "
                f"{_format_number(value)}"
            )
        return lines


class Histogram(_Metric):
    """Cumulative bucket counts plus sum and count, per label set."""

    metric_type = "histogram"

    def __init__(
        self,
        name,
        documentation,
        labelnames=(),
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> None:
        super().__init__(name, documentation, labelnames)
        self.buckets = tuple(sorted(buckets))
        self._counts: Dict[LabelValues, List[int]] = {}
        self._sums: Dict[LabelValues, float] = {}
        self._totals: Dict[LabelValues, int] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[index] += 1
            self._sums[key] = self._sums.get(key, 0.0) + float(value)
            self._totals[key] = self._totals.get(key, 0) + 1

    def count(self, **labels: str) -> int:
        with self._lock:
            return self._totals.get(self._key(labels), 0)

    def sum(self, **labels: str) -> float:
        with self._lock:
            return self._sums.get(self._key(labels), 0.0)

    def render(self) -> List[str]:
        with self._lock:
            keys = sorted(self._totals)
            counts = {k: list(self._counts.get(k, [])) for k in keys}
            sums = dict(self._sums)
            totals = dict(self._totals)
        if not keys:
            return []
        lines = self._header()
        for key in keys:
            for index, bound in enumerate(self.buckets):
                labels = _format_labels(
                    self.labelnames + ("le",), key + (_format_number(bound),)
                )
                lines.append(f"{self.name}_bucket{labels} {counts[key][index]}")
            inf_labels = _format_labels(
                self.labelnames + ("le",), key + ("+Inf",)
            )
            lines.append(f"{self.name}_bucket{inf_labels} {totals[key]}")
            base = _format_labels(self.labelnames, key)
            lines.append(f"{self.name}_sum{base} {_format_number(sums[key])}")
            lines.append(f"{self.name}_count{base} {totals[key]}")
        return lines


class MetricsRegistry:
    """Holds every metric and renders the exposition payload."""

    def __init__(self) -> None:
        self._metrics: Dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def register(self, metric: _Metric) -> _Metric:
        with self._lock:
            existing = self._metrics.get(metric.name)
            if existing is not None:
                return existing
            self._metrics[metric.name] = metric
            return metric

    def counter(self, name, documentation, labelnames=()) -> Counter:
        return self.register(Counter(name, documentation, labelnames))  # type: ignore[return-value]

    def gauge(self, name, documentation, labelnames=()) -> Gauge:
        return self.register(Gauge(name, documentation, labelnames))  # type: ignore[return-value]

    def histogram(
        self, name, documentation, labelnames=(), buckets=DEFAULT_BUCKETS
    ) -> Histogram:
        return self.register(  # type: ignore[return-value]
            Histogram(name, documentation, labelnames, buckets)
        )

    def get(self, name: str) -> Optional[_Metric]:
        with self._lock:
            return self._metrics.get(name)

    def clear(self) -> None:
        """Drop every metric. Used by tests to isolate assertions."""
        with self._lock:
            self._metrics.clear()

    def render(self, extra: Iterable[str] = ()) -> str:
        with self._lock:
            metrics = list(self._metrics.values())
        lines: List[str] = []
        for metric in sorted(metrics, key=lambda m: m.name):
            lines.extend(metric.render())
        lines.extend(extra)
        return "\n".join(lines) + "\n"


registry = MetricsRegistry()

# --------------------------------------------------------------------------- #
# Application metrics
# --------------------------------------------------------------------------- #
http_requests_total = registry.counter(
    "creator_os_http_requests_total",
    "Total HTTP requests processed.",
    ("method", "path", "status"),
)
http_request_duration_seconds = registry.histogram(
    "creator_os_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ("method", "path"),
)
http_requests_in_flight = registry.gauge(
    "creator_os_http_requests_in_flight",
    "HTTP requests currently being served.",
)
http_errors_total = registry.counter(
    "creator_os_http_errors_total",
    "HTTP responses with a 4xx or 5xx status.",
    ("method", "path", "status"),
)

executions_total = registry.counter(
    "creator_os_executions_total",
    "Workflow executions that reached a terminal state.",
    ("status",),
)
execution_duration_seconds = registry.histogram(
    "creator_os_execution_duration_seconds",
    "Workflow execution wall-clock duration in seconds.",
    (),
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900, 3600),
)
nodes_executed_total = registry.counter(
    "creator_os_nodes_executed_total",
    "Workflow nodes executed, by node type and outcome.",
    ("node_type", "status"),
)
node_duration_seconds = registry.histogram(
    "creator_os_node_duration_seconds",
    "Node execution duration in seconds.",
    ("node_type",),
)
execution_queue_depth = registry.gauge(
    "creator_os_execution_queue_depth",
    "Executions waiting in the priority queue.",
)
executions_active = registry.gauge(
    "creator_os_executions_active",
    "Executions currently running.",
)

auth_attempts_total = registry.counter(
    "creator_os_auth_attempts_total",
    "Authentication attempts by outcome.",
    ("outcome",),
)
authz_denials_total = registry.counter(
    "creator_os_authz_denials_total",
    "Requests rejected by a permission check.",
    ("permission",),
)
rate_limit_rejections_total = registry.counter(
    "creator_os_rate_limit_rejections_total",
    "Requests rejected by the rate limiter.",
)

# M9-F1: connection-pool capacity is what caps request concurrency (every
# in-flight request holds a connection for its whole lifetime, see
# app/core/startup.py and docs/DEPLOYMENT.md "Scalability"). Until M9 that
# limit was documented and validated at deploy time but invisible at run time:
# an operator watching /metrics could not see the pool approaching saturation,
# which is the single most likely cause of a stall under load.
db_pool_size = registry.gauge(
    "creator_os_db_pool_size",
    "Connections currently held by the SQLAlchemy pool.",
)
db_pool_checked_out = registry.gauge(
    "creator_os_db_pool_checked_out",
    "Pooled connections currently checked out by in-flight work.",
)
db_pool_available = registry.gauge(
    "creator_os_db_pool_available",
    "Pooled connections idle and immediately available.",
)
db_pool_overflow = registry.gauge(
    "creator_os_db_pool_overflow",
    "Connections created beyond pool_size (negative means unused headroom).",
)
db_pool_capacity = registry.gauge(
    "creator_os_db_pool_capacity",
    "Maximum concurrent connections: DB_POOL_SIZE + DB_MAX_OVERFLOW.",
)
db_pool_utilisation_ratio = registry.gauge(
    "creator_os_db_pool_utilisation_ratio",
    "Checked-out connections divided by total capacity (1.0 = saturated).",
)

app_info = registry.gauge(
    "creator_os_app_info",
    "Build and environment info; the value is always 1.",
    ("version", "environment"),
)
app_start_time_seconds = registry.gauge(
    "creator_os_app_start_time_seconds",
    "Unix timestamp of process start.",
)
app_start_time_seconds.set(time.time())


#: Route templates are used as the ``path`` label instead of raw URLs so that
#: ``/api/workflows/1`` and ``/api/workflows/2`` share one time series rather
#: than producing unbounded label cardinality.
def normalise_path(request) -> str:
    """Return the matched route template, falling back to a safe literal."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return str(path)
    raw = request.url.path
    # Unmatched (404) paths are collapsed so a scanner cannot explode the
    # metric cardinality by requesting random URLs.
    return "/<unmatched>" if raw not in {"/", "/health", "/metrics"} else raw
