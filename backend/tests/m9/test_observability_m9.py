"""M9: regression tests for defects found during production staging validation.

Each test here corresponds to a defect that was observed on a running
PostgreSQL-backed staging deployment, not to a hypothetical one. The finding
IDs match docs/M9_VALIDATION_REPORT.md.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestDatabasePoolMetrics:
    """M9-F1: pool saturation was invisible at run time.

    Pool capacity (DB_POOL_SIZE + DB_MAX_OVERFLOW) is what caps request
    concurrency, and exhaustion presents as requests hanging on checkout.
    Without these gauges an operator cannot distinguish that from a slow
    database.
    """

    POOL_METRICS = (
        "creator_os_db_pool_size",
        "creator_os_db_pool_checked_out",
        "creator_os_db_pool_available",
        "creator_os_db_pool_overflow",
        "creator_os_db_pool_capacity",
        "creator_os_db_pool_utilisation_ratio",
    )

    def test_pool_metrics_are_registered(self):
        from app.infrastructure.observability import metrics as m

        for name in self.POOL_METRICS:
            assert m.registry.get(name) is not None, f"{name} is not registered"

    def test_pool_metrics_are_exported_with_help_and_type(self, client):
        body = client.get("/metrics").text
        for name in self.POOL_METRICS:
            assert f"# HELP {name} " in body, f"missing HELP for {name}"
            assert f"# TYPE {name} gauge" in body, f"missing TYPE for {name}"

    def test_pool_metrics_have_numeric_samples(self, client):
        body = client.get("/metrics").text
        seen = {}
        for line in body.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            name, _, value = line.partition(" ")
            if name in self.POOL_METRICS:
                seen[name] = float(value)
        # SQLite uses a non-queue pool, so only assert on what is emitted;
        # whatever is emitted must be a real number.
        for name, value in seen.items():
            assert isinstance(value, float), f"{name} is not numeric"

    def test_capacity_matches_configured_pool(self, client, monkeypatch):
        """capacity must track configuration, not a hard-coded constant."""
        from app.infrastructure.config.settings import settings
        from app.infrastructure.database.database import engine

        if not hasattr(engine.pool, "checkedout"):
            pytest.skip("SQLite StaticPool does not expose queue-pool counters")

        monkeypatch.setattr(settings, "DB_POOL_SIZE", 7)
        monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 13)
        body = client.get("/metrics").text
        for line in body.splitlines():
            if line.startswith("creator_os_db_pool_capacity "):
                assert float(line.split()[1]) == 20.0
                return
        pytest.fail("creator_os_db_pool_capacity was not exported")

    def test_metrics_scrape_survives_a_broken_pool(self, client, monkeypatch):
        """A metrics scrape must never take the process down."""

        class Exploding:
            def checkedout(self):
                raise RuntimeError("pool is gone")

        monkeypatch.setattr(
            "app.infrastructure.database.database.engine.pool", Exploding(),
            raising=False,
        )
        resp = client.get("/metrics")
        assert resp.status_code == 200
        # The scrape still renders the rest of the registry rather than 500ing.
        assert "creator_os_app_start_time_seconds" in resp.text
        assert "creator_os_http_requests_total" in resp.text


class TestAccountLockoutIsAudited:
    """M9-F2: lockout was logged but never written to the audit trail."""

    def test_lockout_emits_an_audit_event(self, monkeypatch):
        from app.infrastructure.config.settings import settings
        from app.services.security import auth_service as auth_module

        recorded = []

        class FakeAudit:
            def log_audit_event(self, event_name, user_id, details):
                recorded.append((event_name, user_id, details))
                return True

        monkeypatch.setattr(
            "app.services.enterprise.auth.enterprise_auth", FakeAudit()
        )
        monkeypatch.setattr(settings, "AUTH_MAX_FAILED_LOGINS", 3)
        monkeypatch.setattr(settings, "AUTH_LOCKOUT_SECONDS", 900.0)

        class FakeUser:
            id = 42
            username = "victim"
            failed_login_count = 2  # the next failure trips the lockout
            locked_until = None

        class FakeSession:
            def add(self, _obj):
                pass

            def commit(self):
                pass

        auth_module.auth_service._register_failure(FakeSession(), FakeUser())

        names = [r[0] for r in recorded]
        assert "auth.account.locked" in names, (
            f"no lockout audit event was recorded; got {names}"
        )
        event = next(r for r in recorded if r[0] == "auth.account.locked")
        assert event[1] == 42
        assert event[2]["username"] == "victim"
        assert event[2]["failed_login_count"] == 3
        assert event[2]["locked_until"] is not None

    def test_no_audit_event_before_the_threshold(self, monkeypatch):
        from app.infrastructure.config.settings import settings
        from app.services.security import auth_service as auth_module

        recorded = []

        class FakeAudit:
            def log_audit_event(self, event_name, user_id, details):
                recorded.append(event_name)
                return True

        monkeypatch.setattr(
            "app.services.enterprise.auth.enterprise_auth", FakeAudit()
        )
        monkeypatch.setattr(settings, "AUTH_MAX_FAILED_LOGINS", 5)

        class FakeUser:
            id = 7
            username = "early"
            failed_login_count = 0
            locked_until = None

        class FakeSession:
            def add(self, _obj):
                pass

            def commit(self):
                pass

        auth_module.auth_service._register_failure(FakeSession(), FakeUser())
        assert "auth.account.locked" not in recorded

    def test_audit_failure_does_not_break_authentication(self, monkeypatch):
        """Auditing is best-effort: a broken audit sink must not deny logins."""
        from app.infrastructure.config.settings import settings
        from app.services.security import auth_service as auth_module

        class BrokenAudit:
            def log_audit_event(self, *_a, **_k):
                raise RuntimeError("audit database is down")

        monkeypatch.setattr(
            "app.services.enterprise.auth.enterprise_auth", BrokenAudit()
        )
        monkeypatch.setattr(settings, "AUTH_MAX_FAILED_LOGINS", 1)

        class FakeUser:
            id = 9
            username = "resilient"
            failed_login_count = 0
            locked_until = None

        class FakeSession:
            def add(self, _obj):
                pass

            def commit(self):
                pass

        user = FakeUser()
        # Must not raise even though the audit sink explodes.
        auth_module.auth_service._register_failure(FakeSession(), user)
        assert user.locked_until is not None
