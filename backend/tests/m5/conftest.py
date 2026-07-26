"""Shared fixtures for the M5 production-hardening suite.

The M5 tests exercise authentication, which the rest of the suite runs
without. ``auth_settings`` flips ``AUTH_ENABLED`` on for the duration of a test
and supplies a signing secret, so the default single-user behaviour asserted by
the M0-M4 suites is never disturbed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.infrastructure.database.database import get_db


@pytest.fixture
def auth_settings(monkeypatch):
    """Enable authentication with a deterministic signing secret."""
    from app.infrastructure.config.settings import settings

    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        settings, "AUTH_SECRET_KEY", "test-secret-key-that-is-long-enough-1234567890"
    )
    monkeypatch.setattr(settings, "AUTH_ACCESS_TOKEN_TTL_SECONDS", 900.0)
    monkeypatch.setattr(settings, "AUTH_REFRESH_TOKEN_TTL_SECONDS", 86400.0)
    monkeypatch.setattr(settings, "AUTH_ALLOW_SELF_REGISTRATION", False)
    monkeypatch.setattr(settings, "AUTH_BOOTSTRAP_USERNAME", "")
    monkeypatch.setattr(settings, "AUTH_BOOTSTRAP_PASSWORD", "")
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    return settings


@pytest.fixture
def bind_sessions(session_factory, monkeypatch):
    """Point every module-level SessionLocal at the test database."""
    for module in (
        "app.infrastructure.database.database",
        "app.services.workflow.engine",
        "app.services.workflow.streaming",
        "app.services.ai.orchestrator",
        "app.services.media.pipeline",
        "app.services.enterprise.auth",
        "app.services.security.auth_service",
    ):
        try:
            monkeypatch.setattr(f"{module}.SessionLocal", session_factory)
        except AttributeError:
            pass
    return session_factory


@pytest.fixture
def make_client(bind_sessions, session_factory):
    """Build a TestClient against a freshly created app.

    The app is constructed *inside* the fixture rather than at import time so
    that middleware reads whatever settings a test has already monkeypatched.
    """

    def build(**client_kwargs) -> TestClient:
        from app.main import create_app

        app = create_app()

        def override_get_db():
            db = session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        kwargs = {"raise_server_exceptions": False}
        kwargs.update(client_kwargs)
        return TestClient(app, **kwargs)

    return build


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def make_user(bind_sessions, session_factory):
    """Create a user directly in the database."""

    def build(username="tester", password="correct-horse-battery", role="admin"):
        from app.services.security.auth_service import auth_service

        db = session_factory()
        try:
            return auth_service.create_user(
                db, username=username, password=password, role=role
            )
        finally:
            db.close()

    return build


@pytest.fixture
def login(client, make_user):
    """Create a user and return ``(auth_header, tokens, user)``."""

    def build(username="tester", password="correct-horse-battery", role="admin"):
        user = make_user(username=username, password=password, role=role)
        response = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200, response.text
        tokens = response.json()
        header = {"Authorization": f"Bearer {tokens['access_token']}"}
        return header, tokens, user

    return build
