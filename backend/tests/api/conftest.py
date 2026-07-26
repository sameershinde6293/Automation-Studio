"""API test fixtures: a fully wired app bound to an isolated database."""

import pytest
from fastapi.testclient import TestClient

from app.infrastructure.database.database import get_db


@pytest.fixture
def api_client(session_factory, monkeypatch):
    """TestClient against the real app with the DB swapped for a temp one."""
    # Point every module-level SessionLocal reference at the test database so
    # background services (engine, pipeline, auth) hit the same store.
    for module in (
        "app.infrastructure.database.database",
        "app.services.workflow.engine",
        "app.services.ai.orchestrator",
        "app.services.media.pipeline",
        "app.services.enterprise.auth",
    ):
        try:
            monkeypatch.setattr(f"{module}.SessionLocal", session_factory)
        except AttributeError:
            pass

    from app.main import create_app

    app = create_app()

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # ``with TestClient`` runs the lifespan; we skip it to keep tests fast and
    # avoid starting the APScheduler job store against the real database.
    return TestClient(app, raise_server_exceptions=False)
