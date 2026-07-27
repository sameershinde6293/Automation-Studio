"""Regression tests for M6-F6: database connection-pool capacity.

M6 load testing against real PostgreSQL found the API collapsed under
sustained concurrency. Every in-flight request holds a pooled connection for
its entire lifetime (``get_db`` yields the session for the whole handler), so
**pool capacity — not CPU — is what caps request concurrency.**

Measured at 100 concurrent clients, 500 requests, real HTTP + PostgreSQL 16.2:

    capacity  15 (M5 default)  420/500 ok, 16.0% errors,  7.6 rps, p99 60.5s
    capacity  40               460/500 ok,  8.0% errors, 31.2 rps, p99 10.6s
    capacity  60               460/500 ok,  8.0% errors, 31.1 rps, p99 10.7s
    capacity  80 (M6 default)  500/500 ok,  0.0% errors, 81.7 rps, p99  4.6s
    capacity 120               500/500 ok,  0.0% errors, 79.9 rps  (no gain)

Two distinct defects were fixed:

1. **Capacity too small.** Raised the default to 20+60=80, the measured knee.
2. **Exhaustion surfaced as HTTP 500** with a leaked stack trace, and waited
   SQLAlchemy's 30s default. Overload is retryable, so it is now a 10s
   timeout and a 503 + ``Retry-After``, which lets a load balancer back off.

These tests pin the configuration contract and the error-handling contract.
They deliberately do **not** re-run a 100-client load test: that takes minutes
and needs a live server. The load harness lives in ``scripts/loadtest.py`` and
its measured output is recorded in docs/M6_VALIDATION_REPORT.md.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import TimeoutError as SQLTimeoutError

from app.core.startup import SEVERITY_WARNING, validate_settings

#: Starlette's anyio threadpool default. Sync endpoints run there, so this is
#: the number of requests that can be mid-flight (and holding a connection).
ASGI_THREADPOOL_DEFAULT = 40

#: The measured knee of the throughput curve; see the module docstring.
MIN_RECOMMENDED_POOL_CAPACITY = 80


class TestPoolDefaults:
    """The shipped defaults must be able to serve the concurrency we admit."""

    def test_capacity_covers_the_asgi_threadpool(self):
        from app.infrastructure.config.settings import Settings

        settings = Settings()
        capacity = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
        assert capacity >= ASGI_THREADPOOL_DEFAULT, (
            f"pool capacity {capacity} is below the {ASGI_THREADPOOL_DEFAULT} "
            "requests the ASGI threadpool will admit concurrently; excess "
            "requests queue on connection checkout instead of being served"
        )

    def test_capacity_meets_the_measured_knee(self):
        from app.infrastructure.config.settings import Settings

        settings = Settings()
        capacity = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
        assert capacity >= MIN_RECOMMENDED_POOL_CAPACITY, (
            f"pool capacity {capacity} is below the measured knee of "
            f"{MIN_RECOMMENDED_POOL_CAPACITY}; M6 measured 16% errors and "
            "7.6 rps at 100 concurrent clients below this point"
        )

    def test_pool_timeout_is_explicit_and_short(self):
        """SQLAlchemy's 30s default turns overload into a 30s hang per request."""
        from app.infrastructure.config.settings import Settings

        timeout = Settings().DB_POOL_TIMEOUT_SECONDS
        assert 0 < timeout <= 15, (
            f"DB_POOL_TIMEOUT_SECONDS={timeout} is too long; overload should "
            "shed fast so the process stays responsive"
        )


class TestEngineAppliesPoolSettings:
    """A setting nobody wires into the engine is documentation, not config."""

    def test_engine_kwargs_include_pool_timeout(self, monkeypatch):
        from app.infrastructure.config.settings import settings
        from app.infrastructure.database import database

        monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+psycopg://u:p@h/db")
        monkeypatch.setattr(settings, "DB_POOL_SIZE", 20)
        monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 60)
        monkeypatch.setattr(settings, "DB_POOL_TIMEOUT_SECONDS", 10.0)

        kwargs = database._engine_kwargs()
        assert kwargs["pool_size"] == 20
        assert kwargs["max_overflow"] == 60
        assert kwargs["pool_timeout"] == 10.0
        assert kwargs["pool_pre_ping"] is True

    def test_sqlite_does_not_get_pool_arguments(self, monkeypatch):
        """SQLite uses its own pooling; passing these would raise."""
        from app.infrastructure.config.settings import settings
        from app.infrastructure.database import database

        monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///./x.db")
        kwargs = database._engine_kwargs()
        assert "pool_timeout" not in kwargs
        assert "max_overflow" not in kwargs


class TestStartupWarnsOnUndersizedPool:
    """An operator who shrinks the pool should be told what it costs."""

    class _FakeSettings:
        ENVIRONMENT = "production"
        AUTH_ENABLED = True
        AUTH_SECRET_KEY = "k" * 48
        AUTH_BOOTSTRAP_PASSWORD = ""
        ENABLE_DOCS = False
        CORS_ORIGINS = ["https://app.example.com"]
        ALLOWED_HOSTS = ["app.example.com"]
        RATE_LIMIT_ENABLED = True
        SECURITY_HSTS_ENABLED = True
        DATABASE_URL = "postgresql+psycopg://u:p@h/db"
        DB_ECHO = False
        DB_POOL_SIZE = 5
        DB_MAX_OVERFLOW = 10
        ALLOW_SHELL_EXECUTOR = False
        SHELL_ALLOWED_COMMANDS: list = []
        ALLOW_PYTHON_EXECUTOR = False
        ALLOW_JAVASCRIPT_EXECUTOR = False
        ALLOW_DATABASE_EXECUTOR = False
        SCRIPT_SANDBOX_ENABLED = True
        HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS = False
        is_production = True
        is_sqlite = False

    def _findings(self, **overrides):
        settings = self._FakeSettings()
        for key, value in overrides.items():
            setattr(settings, key, value)
        return validate_settings(settings)

    def test_undersized_pool_produces_a_warning(self):
        findings = self._findings(DB_POOL_SIZE=5, DB_MAX_OVERFLOW=10)
        pool = [f for f in findings if f.key == "DB_POOL_SIZE"]
        assert pool, "an undersized pool should be reported at startup"
        assert pool[0].severity == SEVERITY_WARNING
        assert pool[0].remediation

    def test_adequately_sized_pool_produces_no_finding(self):
        findings = self._findings(DB_POOL_SIZE=20, DB_MAX_OVERFLOW=60)
        assert [f for f in findings if f.key == "DB_POOL_SIZE"] == []

    def test_pool_warning_never_blocks_startup(self):
        """Capacity is a tuning concern, not a security one — warn, never fail."""
        findings = self._findings(DB_POOL_SIZE=1, DB_MAX_OVERFLOW=1)
        pool = [f for f in findings if f.key == "DB_POOL_SIZE"]
        assert pool and pool[0].severity == SEVERITY_WARNING

    def test_sqlite_is_not_warned_about_pooling(self):
        findings = self._findings(
            is_sqlite=True, DATABASE_URL="sqlite:///./x.db", DB_POOL_SIZE=5
        )
        assert [f for f in findings if f.key == "DB_POOL_SIZE"] == []


class TestPoolExhaustionIsRetryable:
    """Exhaustion is overload, and must be reported as such."""

    @pytest.fixture
    def exhausting_client(self, make_client, monkeypatch):
        """An app whose DB dependency always reports the pool as exhausted."""
        from app.infrastructure.database.database import get_db

        client = make_client()

        def boom():
            raise SQLTimeoutError(
                "QueuePool limit of size 20 overflow 60 reached, "
                "connection timed out, timeout 10.00"
            )

        client.app.dependency_overrides[get_db] = boom
        return client

    def test_returns_503_not_500(self, exhausting_client):
        """Pre-M6 this was an opaque 500, which reads as a server bug."""
        response = exhausting_client.get("/api/workflows/")
        assert response.status_code == 503

    def test_sets_retry_after(self, exhausting_client):
        response = exhausting_client.get("/api/workflows/")
        assert "Retry-After" in response.headers

    def test_uses_a_stable_error_code(self, exhausting_client):
        body = exhausting_client.get("/api/workflows/").json()
        assert body["error"]["code"] == "database_unavailable"

    def test_does_not_leak_internal_detail(self, exhausting_client):
        """The client must not learn the pool geometry."""
        text = exhausting_client.get("/api/workflows/").text
        assert "QueuePool" not in text
        assert "sqlalchemy" not in text.lower()
        assert "Traceback" not in text
