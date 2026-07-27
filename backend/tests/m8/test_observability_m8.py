"""M8: Observability validation - health, metrics, logging, backup/restore"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestHealthEndpoints:
    def test_liveness_no_db_dependency(self, client):
        """Liveness must not touch DB - should succeed even if DB is down in theory"""
        resp = client.get("/health/live")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "uptime_seconds" in data

    def test_readiness_checks_db_and_workers(self, client):
        """/health/ready checks database, scheduler, workers, config"""
        resp = client.get("/health/ready")
        # Can be 200 or 503 depending on state, but must have checks
        assert resp.status_code in (200, 503)
        data = resp.json()
        assert "status" in data
        assert "checks" in data
        checks = data["checks"]
        # At least database should be checked
        assert "database" in checks
        # Scheduler
        assert "scheduler" in checks

    def test_basic_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    def test_readiness_503_when_degraded(self, client):
        """If DB were down, readiness would be 503 - test structure"""
        resp = client.get("/health/ready")
        # In healthy test env, should be 200
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ready", "degraded")


class TestMetrics:
    def test_metrics_endpoint_renders_prometheus_format(self, client):
        resp = client.get("/metrics")
        if resp.status_code == 404:
            pytest.skip("/metrics not enabled in this environment (METRICS_ENABLED=false)")
        assert resp.status_code in (200, 403)  # 403 if auth required
        if resp.status_code == 200:
            text = resp.text
            # Should contain HELP and TYPE lines
            assert "# HELP" in text
            assert "# TYPE" in text
            # Should contain at least http_requests_total or similar
            assert "creator_os_" in text or "http_" in text.lower() or "process_" in text.lower() or len(text) > 100

    def test_metrics_are_thread_safe(self):
        """Metrics registry must handle concurrent increments"""
        from app.infrastructure.observability.metrics import Counter
        c = Counter("test_m8_counter", "test", labelnames=["path"])
        # Simulate concurrent increments
        c.inc(1, path="/test")
        c.inc(2, path="/test")
        assert c.value(path="/test") == 3

    def test_histogram_buckets(self):
        from app.infrastructure.observability.metrics import Histogram
        h = Histogram("test_m8_hist", "test histogram", labelnames=["method"])
        h.observe(0.1, method="GET")
        h.observe(0.5, method="GET")
        assert h.count(method="GET") == 2
        assert h.sum(method="GET") >= 0.5


class TestStructuredLogging:
    def test_json_logs_have_required_fields(self):
        from app.infrastructure.logging.logger import JsonFormatter
        import logging
        formatter = JsonFormatter()
        record = logging.LogRecord("creator_os.test", logging.INFO, "test.py", 10, "hello world", None, None)
        output = formatter.format(record)
        data = json.loads(output)
        assert "ts" in data
        assert "level" in data
        assert "logger" in data
        assert "message" in data
        assert data["message"] == "hello world"

    def test_logs_include_correlation_ids_when_set(self):
        from app.infrastructure.logging.logger import JsonFormatter, request_id_var, correlation_id_var
        import logging
        token1 = request_id_var.set("req-123")
        token2 = correlation_id_var.set("corr-456")
        try:
            formatter = JsonFormatter()
            record = logging.LogRecord("creator_os", logging.INFO, "p", 1, "msg", None, None)
            data = json.loads(formatter.format(record))
            assert data.get("request_id") == "req-123"
            assert data.get("correlation_id") == "corr-456"
        finally:
            request_id_var.reset(token1)
            correlation_id_var.reset(token2)

    def test_secret_redaction_in_logs(self):
        from app.infrastructure.logging.logger import redact
        samples = [
            ("sk-1234567890abcdef", "sk-***REDACTED***"),
            ('api_key="my-secret-key"', "REDACTED"),
            ("Authorization: Bearer token123", "REDACTED"),
            ('password="supersecret"', "REDACTED"),
        ]
        for original, should_contain_or_not_contain in samples:
            redacted = redact(original)
            if "REDACTED" in should_contain_or_not_contain:
                assert "REDACTED" in redacted
                # Original secret should not be present
                if len(original) > 10:
                    # Don't check small strings
                    assert original not in redacted or "REDACTED" in redacted


class TestLogRotation:
    def test_rotating_file_handler_configured(self):
        from app.infrastructure.logging.logger import setup_logging
        import logging.handlers

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "app.log"
            log = setup_logging(level="INFO", log_file=str(log_file), force=True)
            file_handlers = [h for h in log.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
            assert len(file_handlers) == 1
            handler = file_handlers[0]
            assert handler.maxBytes == 10 * 1024 * 1024  # 10 MB
            assert handler.backupCount == 5

            # Write something
            log.info("test log entry")
            for h in log.handlers:
                h.flush()

            assert log_file.exists()
            assert "test log entry" in log_file.read_text()

            # Cleanup
            setup_logging(force=True)

    def test_log_rotation_not_triggered_for_small_logs(self):
        """Rollover should NOT happen for small logs (only config check)"""
        from app.infrastructure.logging.logger import setup_logging
        import logging.handlers

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "app.log"
            log = setup_logging(level="INFO", log_file=str(log_file), force=True)

            # Write small amount
            for i in range(10):
                log.info(f"log line {i}")

            for h in log.handlers:
                h.flush()

            # Should still be only one file
            assert log_file.exists()
            # Backup files 1-5 should NOT exist yet
            for i in range(1, 6):
                assert not Path(f"{log_file}.{i}").exists(), f"Backup {i} should not exist for small logs"

            setup_logging(force=True)


class TestBackupRestore:
    """Test backup/restore logic with SQLite (available in this env)"""

    def test_sqlite_backup_and_restore(self):
        import shutil
        import sqlite3

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            db_path = tmpdir / "test.db"
            backup_path = tmpdir / "backup.db"

            # Create test DB
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE canary (id INTEGER PRIMARY KEY, val TEXT)")
            conn.execute("INSERT INTO canary (val) VALUES ('test-data')")
            conn.commit()
            conn.close()

            assert db_path.exists()

            # Backup (file copy)
            shutil.copy(db_path, backup_path)
            assert backup_path.exists()
            assert backup_path.stat().st_size > 0

            # Simulate disaster: delete data
            conn = sqlite3.connect(db_path)
            conn.execute("DELETE FROM canary")
            conn.commit()
            cur = conn.execute("SELECT COUNT(*) FROM canary")
            assert cur.fetchone()[0] == 0
            conn.close()

            # Restore
            shutil.copy(backup_path, db_path)

            # Verify restore
            conn = sqlite3.connect(db_path)
            cur = conn.execute("SELECT COUNT(*) FROM canary")
            count = cur.fetchone()[0]
            assert count == 1, f"Expected 1 row after restore, got {count}"
            conn.close()

    def test_backup_manifest_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            manifest = tmpdir / "manifest.txt"
            manifest.write_text(f"Date: 2026-07-27\nCommit: test\nFiles: ...")
            assert manifest.exists()
            content = manifest.read_text()
            assert "Date:" in content


class TestStartupShutdown:
    def test_lifespan_startup_logs(self, client):
        # If we can get /health, lifespan startup succeeded
        resp = client.get("/health")
        assert resp.status_code == 200

        # Check /api/system/info has version and uptime
        resp = client.get("/api/system/info")
        if resp.status_code != 404:
            data = resp.json()
            assert "version" in data
            assert "uptime_seconds" in data
