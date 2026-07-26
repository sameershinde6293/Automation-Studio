"""M2 API fixtures."""

import pytest
from fastapi.testclient import TestClient

from app.infrastructure.database.database import get_db


@pytest.fixture
def api_client(session_factory, monkeypatch):
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
    return TestClient(app, raise_server_exceptions=False)
