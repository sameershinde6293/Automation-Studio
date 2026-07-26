"""Shared pytest fixtures.

Every test runs against an isolated in-memory SQLite database so suites cannot
leak state into one another (a fragility in the V1.0 suite).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.infrastructure.database.database import Base  # noqa: E402


def make_test_engine():
    """Create a fresh, isolated in-memory SQLite engine."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from app.infrastructure.database.database import apply_sqlite_pragmas
    from sqlalchemy import event

    event.listen(engine, "connect", apply_sqlite_pragmas)
    return engine


@pytest.fixture
def db_engine():
    import app.domain.models  # noqa: F401  register all models

    engine = make_test_engine()
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def session_factory(db_engine):
    return sessionmaker(
        autocommit=False, autoflush=False, bind=db_engine, expire_on_commit=False
    )


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def reset_event_bus():
    """Keep event subscriptions from leaking between tests."""
    from app.infrastructure.events.event_bus import event_bus

    event_bus.clear()
    yield
    event_bus.clear()


@pytest.fixture(autouse=True)
def reset_plugin_sdk():
    from app.services.plugin_sdk.sdk import plugin_sdk

    plugin_sdk.clear_hooks()
    yield
    plugin_sdk.clear_hooks()


@pytest.fixture
def tmp_media_root(tmp_path, monkeypatch):
    """Point the media pipeline at an isolated temp directory."""
    from app.infrastructure.config.settings import settings

    root = tmp_path / "media"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "MEDIA_ROOT", str(root))
    return root
