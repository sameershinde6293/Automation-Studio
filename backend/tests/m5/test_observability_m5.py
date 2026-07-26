"""M5: health probes, Prometheus metrics and error aggregation."""

from __future__ import annotations

import pytest

from app.infrastructure.observability.errors import ErrorAggregator
from app.infrastructure.observability.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    normalise_path,
)


class TestHealthProbes:
    def test_liveness_reports_uptime(self, client):
        response = client.get("/health/live")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["uptime_seconds"] >= 0

    def test_legacy_health_endpoint_is_unchanged(self, client):
        """V1.0 clients poll this exact shape."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_readiness_reports_dependency_checks(self, client):
        response = client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        checks = body["checks"]
        assert checks["database"] == "ok"
        assert "scheduler" in checks
        assert "execution_workers" in checks
        assert "configuration" in checks

    def test_readiness_returns_503_when_the_database_is_down(
        self, make_client, monkeypatch
    ):
        """An orchestrator must pull a broken instance out of rotation."""
        import app.infrastructure.database.database as database_module

        class _BrokenEngine:
            def connect(self):
                raise RuntimeError("database is unreachable")

        monkeypatch.setattr(database_module, "engine", _BrokenEngine())
        response = make_client().get("/health/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert "error" in response.json()["checks"]["database"]

    def test_probes_are_not_rate_limited(self, make_client, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(settings, "RATE_LIMIT_REQUESTS", 2)
        client = make_client()
        for _ in range(10):
            assert client.get("/health/live").status_code == 200


class TestMetricsEndpoint:
    def test_exposition_is_prometheus_text_format(self, client):
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        body = response.text
        assert "# HELP" in body and "# TYPE" in body

    def test_requests_are_counted(self, client):
        client.get("/api/system/info")
        body = client.get("/metrics").text
        assert "creator_os_http_requests_total" in body
        assert "creator_os_http_request_duration_seconds" in body

    def test_path_label_uses_the_route_template(self, client):
        """Raw ids would make metric cardinality unbounded."""
        client.get("/api/workflows/12345")
        body = client.get("/metrics").text
        assert "12345" not in body

    def test_unmatched_paths_are_collapsed(self, client):
        """A scanner must not be able to explode cardinality."""
        client.get("/definitely-not-a-route-xyz")
        body = client.get("/metrics").text
        assert "definitely-not-a-route-xyz" not in body

    def test_can_be_disabled(self, make_client, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "METRICS_ENABLED", False)
        assert make_client().get("/metrics").status_code == 404

    def test_can_require_authentication(self, make_client, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "METRICS_REQUIRE_AUTH", True)
        monkeypatch.setattr(settings, "AUTH_ENABLED", True)
        monkeypatch.setattr(settings, "AUTH_SECRET_KEY", "x" * 48)
        assert make_client().get("/metrics").status_code == 403


class TestMetricPrimitives:
    def test_counter_accumulates(self):
        counter = Counter("c_total", "help", ("label",))
        counter.inc(label="a")
        counter.inc(2, label="a")
        counter.inc(label="b")
        assert counter.value(label="a") == 3
        assert counter.value(label="b") == 1

    def test_counter_refuses_to_decrease(self):
        with pytest.raises(ValueError):
            Counter("c_total", "help").inc(-1)

    def test_gauge_moves_both_ways(self):
        gauge = Gauge("g", "help")
        gauge.set(5)
        gauge.inc(2)
        gauge.dec(3)
        assert gauge.value() == 4

    def test_histogram_buckets_are_cumulative(self):
        histogram = Histogram("h", "help", (), buckets=(1, 5, 10))
        for value in (0.5, 2, 7, 20):
            histogram.observe(value)
        assert histogram.count() == 4
        assert histogram.sum() == 29.5
        rendered = "\n".join(histogram.render())
        assert 'le="1"' in rendered and 'le="+Inf"' in rendered

    def test_missing_labels_are_rejected(self):
        with pytest.raises(ValueError):
            Counter("c_total", "help", ("expected",)).inc()

    def test_label_values_are_escaped(self):
        counter = Counter("c_total", "help", ("label",))
        counter.inc(label='has"quote')
        assert '\\"' in "\n".join(counter.render())

    def test_registry_renders_registered_metrics(self):
        registry = MetricsRegistry()
        registry.counter("a_total", "help").inc()
        assert "a_total" in registry.render()

    def test_registering_the_same_name_twice_returns_the_original(self):
        registry = MetricsRegistry()
        first = registry.counter("dup_total", "help")
        second = registry.counter("dup_total", "help")
        assert first is second

    def test_registry_is_thread_safe(self):
        """Metrics are written from request handlers and worker threads."""
        import threading

        registry = MetricsRegistry()
        counter = registry.counter("threaded_total", "help")

        def worker():
            for _ in range(200):
                counter.inc()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert counter.value() == 1600


class TestErrorAggregation:
    def test_identical_errors_group_together(self):
        aggregator = ErrorAggregator()
        for _ in range(3):
            try:
                raise ValueError("the same failure")
            except ValueError as exc:
                aggregator.record(exc, path="/api/x")
        groups = aggregator.top()
        assert len(groups) == 1
        assert groups[0]["count"] == 3

    def test_different_errors_stay_separate(self):
        aggregator = ErrorAggregator()
        try:
            raise ValueError("first")
        except ValueError as exc:
            aggregator.record(exc)
        try:
            raise KeyError("second")
        except KeyError as exc:
            aggregator.record(exc)
        assert len(aggregator.top()) == 2

    def test_group_count_is_bounded(self):
        """Memory must stay bounded regardless of error variety."""
        aggregator = ErrorAggregator(max_groups=5)
        for index in range(50):
            try:
                raise ValueError(f"distinct failure {index}")
            except ValueError as exc:
                aggregator.record(exc)
        assert len(aggregator.top(limit=100)) <= 5

    def test_summary_counts_every_occurrence(self):
        aggregator = ErrorAggregator()
        for _ in range(4):
            try:
                raise ValueError("boom")
            except ValueError as exc:
                aggregator.record(exc)
        summary = aggregator.summary()
        assert summary["total_errors"] == 4
        assert summary["distinct_errors"] == 1

    def test_context_is_retained_on_samples(self):
        aggregator = ErrorAggregator()
        try:
            raise ValueError("boom")
        except ValueError as exc:
            aggregator.record(exc, request_id="req-1", path="/api/x", method="POST")
        group = aggregator.top()[0]
        assert group["last_request_id"] == "req-1"
        assert group["last_path"] == "/api/x"

    def test_unhandled_api_errors_are_aggregated(self, make_client):
        """End-to-end: a 500 must show up in the aggregator."""
        from app.infrastructure.observability.errors import error_aggregator
        from app.main import create_app

        error_aggregator.clear()
        app = create_app()

        @app.get("/api/_boom")
        def boom():
            raise RuntimeError("deliberate failure for the test")

        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/api/_boom").status_code == 500
        assert error_aggregator.summary()["total_errors"] >= 1
        error_aggregator.clear()

    def test_internal_errors_do_not_leak_details(self, make_client):
        from app.main import create_app

        app = create_app()

        @app.get("/api/_boom2")
        def boom():
            raise RuntimeError("SECRET-INTERNAL-DETAIL")

        from fastapi.testclient import TestClient

        response = TestClient(app, raise_server_exceptions=False).get("/api/_boom2")
        assert response.status_code == 500
        assert "SECRET-INTERNAL-DETAIL" not in response.text
        assert response.json()["error"]["code"] == "internal_error"
