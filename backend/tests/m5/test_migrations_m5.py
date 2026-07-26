"""M5: Alembic migration tests.

The M5 audit found that migrations had never been executed in CI, and that
``audit_events`` had no migration at all -- a migration-only deployment (the
documented production path) started without the table, so audit writes failed
at runtime. These tests run the real migration chain against a temporary
SQLite database.

They are marked ``slow`` because each one drives Alembic end to end.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

BACKEND_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.slow


def run_alembic(*args, database_url: str):
    """Invoke the Alembic CLI the way a deployment would."""
    import os

    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.fixture
def migrated_db(tmp_path):
    """A database at the latest revision, plus its URL."""
    db_path = tmp_path / "migrations.db"
    url = f"sqlite:///{db_path}"
    result = run_alembic("upgrade", "head", database_url=url)
    assert result.returncode == 0, f"upgrade failed:\n{result.stderr}"
    return url


class TestUpgrade:
    def test_upgrade_to_head_succeeds_from_empty(self, migrated_db):
        engine = create_engine(migrated_db)
        try:
            tables = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()
        assert "alembic_version" in tables

    def test_every_orm_table_exists_after_migrating(self, migrated_db):
        """Guards against a model being added without a migration.

        This is exactly the defect that left ``audit_events`` missing.
        """
        import app.domain.models  # noqa: F401  registers all models
        from app.infrastructure.database.database import Base

        engine = create_engine(migrated_db)
        try:
            actual = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

        expected = set(Base.metadata.tables)
        missing = expected - actual
        assert not missing, f"tables defined as models but never migrated: {missing}"

    def test_audit_events_is_created_by_migration(self, migrated_db):
        """M4 known issue #10, fixed in M5."""
        engine = create_engine(migrated_db)
        try:
            inspector = inspect(engine)
            assert "audit_events" in inspector.get_table_names()
            columns = {c["name"] for c in inspector.get_columns("audit_events")}
        finally:
            engine.dispose()
        assert {"id", "user_id", "event_name", "details", "created_at"} <= columns

    def test_identity_tables_are_created(self, migrated_db):
        engine = create_engine(migrated_db)
        try:
            tables = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()
        assert {"users", "api_keys", "refresh_sessions"} <= tables

    def test_identity_uniqueness_and_indexes(self, migrated_db):
        engine = create_engine(migrated_db)
        try:
            inspector = inspect(engine)
            user_indexes = {i["name"] for i in inspector.get_indexes("users")}
            key_indexes = {i["name"] for i in inspector.get_indexes("api_keys")}
        finally:
            engine.dispose()
        assert "ix_users_username" in user_indexes
        assert "ix_api_keys_key_hash" in key_indexes

    def test_foreign_keys_cascade_from_users(self, migrated_db):
        """Deleting a user must not orphan their keys and sessions."""
        engine = create_engine(migrated_db)
        try:
            inspector = inspect(engine)
            for table in ("api_keys", "refresh_sessions"):
                foreign_keys = inspector.get_foreign_keys(table)
                assert foreign_keys, f"{table} has no foreign key to users"
                assert foreign_keys[0]["referred_table"] == "users"
                assert foreign_keys[0]["options"].get("ondelete") == "CASCADE"
        finally:
            engine.dispose()


class TestDowngradeAndIdempotency:
    def test_downgrade_then_upgrade_round_trips(self, migrated_db):
        down = run_alembic("downgrade", "-1", database_url=migrated_db)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"

        engine = create_engine(migrated_db)
        try:
            after_downgrade = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()
        assert "users" not in after_downgrade
        # audit_events predates M5 as a model, so the downgrade must preserve it.
        assert "audit_events" in after_downgrade

        up = run_alembic("upgrade", "head", database_url=migrated_db)
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"

        engine = create_engine(migrated_db)
        try:
            restored = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()
        assert {"users", "api_keys", "refresh_sessions"} <= restored

    def test_upgrade_is_idempotent(self, migrated_db):
        result = run_alembic("upgrade", "head", database_url=migrated_db)
        assert result.returncode == 0

    def test_migration_survives_a_create_all_database(self, tmp_path):
        """A dev instance built by create_all() must still accept the migration.

        ``audit_events`` already exists there, so the migration has to detect
        that rather than failing with "table already exists".
        """
        db_path = tmp_path / "createall.db"
        url = f"sqlite:///{db_path}"

        import app.domain.models  # noqa: F401
        from app.infrastructure.database.database import Base

        engine = create_engine(url)
        try:
            Base.metadata.create_all(bind=engine)
        finally:
            engine.dispose()

        result = run_alembic("stamp", "c4e7a1b90d52", database_url=url)
        assert result.returncode == 0, result.stderr
        result = run_alembic("upgrade", "head", database_url=url)
        assert result.returncode == 0, f"upgrade over create_all failed:\n{result.stderr}"


class TestRevisionChain:
    def test_there_is_exactly_one_head(self):
        """Two heads mean a branched history that cannot be applied linearly."""
        result = run_alembic("heads", database_url="sqlite:///:memory:")
        assert result.returncode == 0, result.stderr
        heads = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(heads) == 1, f"expected a single head, got: {heads}"
