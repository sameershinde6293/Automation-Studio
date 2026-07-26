"""Database layer tests: pragmas, sessions, rollback semantics, repositories."""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.repositories.project_repository import (
    ProjectCreate,
    ProjectUpdate,
    project_repo,
)
from app.infrastructure.database.database import (
    Base,
    SessionLocal,
    apply_sqlite_pragmas,
    engine,
    get_db,
    session_scope,
)


class TestEngineConfiguration:
    def test_engine_exists(self):
        assert engine is not None

    def test_base_metadata_registered(self):
        import app.domain.models  # noqa: F401

        tables = set(Base.metadata.tables)
        for expected in (
            "projects", "workflows", "workflow_nodes", "workflow_edges",
            "workflow_executions", "node_executions", "plugins",
            "audit_events", "media_assets", "ai_conversations",
        ):
            assert expected in tables

    def test_session_local_creates_sessions(self):
        session = SessionLocal()
        assert isinstance(session, Session)
        session.close()


class TestSqlitePragmas:
    def test_wal_journal_mode_enabled(self, db_engine):
        with db_engine.connect() as connection:
            mode = connection.execute(text("PRAGMA journal_mode")).scalar()
        # In-memory databases report 'memory'; file-backed report 'wal'.
        assert str(mode).lower() in {"wal", "memory"}

    def test_foreign_keys_enforced(self, db_engine):
        with db_engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1

    def test_busy_timeout_set(self, db_engine):
        with db_engine.connect() as connection:
            assert connection.execute(text("PRAGMA busy_timeout")).scalar() > 0

    def test_apply_pragmas_survives_bad_connection(self):
        class BrokenCursor:
            def execute(self, *_a):
                raise RuntimeError("nope")

            def close(self):
                pass

        class BrokenConnection:
            def cursor(self):
                return BrokenCursor()

        apply_sqlite_pragmas(BrokenConnection())  # must not raise


class TestGetDb:
    def test_yields_session_and_closes(self):
        generator = get_db()
        session = next(generator)
        assert isinstance(session, Session)
        generator.close()

    def test_rolls_back_on_exception(self, monkeypatch, session_factory):
        """V1.0 leaked a dirty session when the handler raised."""
        monkeypatch.setattr(
            "app.infrastructure.database.database.SessionLocal", session_factory
        )
        rolled_back = {"value": False}
        real_factory = session_factory

        class TrackingSession(Session):
            def rollback(self):
                rolled_back["value"] = True
                super().rollback()

        session = real_factory()
        monkeypatch.setattr(
            "app.infrastructure.database.database.SessionLocal", lambda: session
        )
        original_rollback = session.rollback
        session.rollback = lambda: (rolled_back.update(value=True), original_rollback())

        generator = get_db()
        next(generator)
        with pytest.raises(RuntimeError):
            generator.throw(RuntimeError("handler failed"))
        assert rolled_back["value"] is True


class TestSessionScope:
    def test_commits_on_success(self, monkeypatch, session_factory, db_engine):
        monkeypatch.setattr(
            "app.infrastructure.database.database.SessionLocal", session_factory
        )
        with session_scope() as session:
            project_repo.create(session, ProjectCreate(name="Scoped"))
        verify = session_factory()
        assert verify.query(project_repo.model).count() == 1
        verify.close()

    def test_rolls_back_on_error(self, monkeypatch, session_factory, db_engine):
        monkeypatch.setattr(
            "app.infrastructure.database.database.SessionLocal", session_factory
        )
        with pytest.raises(RuntimeError):
            with session_scope() as session:
                session.add(project_repo.model(name="Doomed"))
                raise RuntimeError("fail")
        verify = session_factory()
        assert verify.query(project_repo.model).count() == 0
        verify.close()


class TestBaseRepository:
    def test_create_and_get(self, db):
        created = project_repo.create(db, ProjectCreate(name="P1", description="d"))
        fetched = project_repo.get(db, created.id)
        assert fetched.name == "P1"

    def test_get_missing_returns_none(self, db):
        assert project_repo.get(db, 99999) is None

    def test_get_all_pagination(self, db):
        for i in range(5):
            project_repo.create(db, ProjectCreate(name=f"P{i}"))
        assert len(project_repo.get_all(db, skip=0, limit=2)) == 2
        assert len(project_repo.get_all(db, skip=4, limit=10)) == 1

    def test_count(self, db):
        assert project_repo.count(db) == 0
        project_repo.create(db, ProjectCreate(name="x"))
        assert project_repo.count(db) == 1

    def test_exists(self, db):
        created = project_repo.create(db, ProjectCreate(name="x"))
        assert project_repo.exists(db, created.id) is True
        assert project_repo.exists(db, 99999) is False

    def test_create_many(self, db):
        created = project_repo.create_many(
            db, [ProjectCreate(name="a"), ProjectCreate(name="b")]
        )
        assert len(created) == 2
        assert project_repo.count(db) == 2

    def test_update_partial(self, db):
        created = project_repo.create(db, ProjectCreate(name="Old", description="keep"))
        updated = project_repo.update(db, created, ProjectUpdate(name="New"))
        assert updated.name == "New"
        assert updated.description == "keep"

    def test_update_accepts_dict(self, db):
        created = project_repo.create(db, ProjectCreate(name="Old"))
        updated = project_repo.update(db, created, {"name": "Dict"})
        assert updated.name == "Dict"

    def test_update_ignores_unknown_fields(self, db):
        created = project_repo.create(db, ProjectCreate(name="X"))
        updated = project_repo.update(db, created, {"not_a_column": 1, "name": "Y"})
        assert updated.name == "Y"

    def test_delete(self, db):
        created = project_repo.create(db, ProjectCreate(name="Gone"))
        assert project_repo.delete(db, created.id) is not None
        assert project_repo.get(db, created.id) is None

    def test_delete_missing_returns_none(self, db):
        assert project_repo.delete(db, 99999) is None

    def test_timestamps_populated(self, db):
        created = project_repo.create(db, ProjectCreate(name="T"))
        assert created.created_at is not None
        assert created.updated_at is not None


class TestCascades:
    def test_deleting_workflow_cascades_to_nodes(self, db):
        from app.domain.repositories.workflow_repository import (
            NodeCreate,
            WorkflowCreate,
            node_repo,
            workflow_repo,
        )

        workflow = workflow_repo.create(db, WorkflowCreate(name="W"))
        node_repo.create(
            db, NodeCreate(workflow_id=workflow.id, name="n", node_type="dummy")
        )
        workflow_repo.delete(db, workflow.id)
        assert node_repo.get_by_workflow(db, workflow.id) == []
