"""Regression tests for M6-F3: PostgreSQL migration round-trips.

``tests/m5/test_migrations_m5.py`` exercises the migration chain against
SQLite, which is why it never caught this. SQLite has no native ENUM type;
PostgreSQL implements ``sa.Enum`` as a standalone ``TYPE`` that survives
``DROP TABLE``. Two downgrades dropped their tables but not their types, so
the rollback procedure documented in docs/DEPLOYMENT.md wedged the database:

    alembic downgrade base && alembic upgrade head
    -> psycopg.errors.DuplicateObject: type "executionstatus" already exists

PostgreSQL is the only supported production database, so this broke rollback
on every real deployment. See docs/M6_VALIDATION_REPORT.md finding M6-F3.

These tests need a real PostgreSQL server and **skip** when one is not
reachable, so the suite stays runnable on a laptop with no database installed.
Point ``TEST_POSTGRES_URL`` at a scratch database to enable them; CI should
set it. They were developed and verified against PostgreSQL 16.2.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

BACKEND_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.slow, pytest.mark.integration]

#: Enum types the schema creates on PostgreSQL. A downgrade to base must leave
#: none of them behind, or the next upgrade fails.
SCHEMA_ENUM_TYPES = ("executionstatus", "loglevel")


def _postgres_url() -> str | None:
    """Return a usable PostgreSQL URL, or None to skip.

    Verifies the server actually answers rather than trusting the variable, so
    a stale export produces a skip instead of a confusing connection error.
    """
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        return None
    try:
        engine = create_engine(url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()
    except Exception:
        return None
    return url


POSTGRES_URL = _postgres_url()

pytestmark.append(
    pytest.mark.skipif(
        POSTGRES_URL is None,
        reason="Set TEST_POSTGRES_URL to a scratch PostgreSQL database to run "
        "the M6 PostgreSQL migration tests.",
    )
)


def run_alembic(*args, database_url: str):
    """Invoke the Alembic CLI exactly the way a deployment release step does."""
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.fixture
def clean_pg():
    """A completely empty public schema, restored again afterwards."""
    engine = create_engine(POSTGRES_URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    yield POSTGRES_URL
    engine = create_engine(POSTGRES_URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def enum_types(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT t.typname FROM pg_type t "
                    "JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE n.nspname = 'public' AND t.typtype = 'e'"
                )
            )
            return {row[0] for row in rows}
    finally:
        engine.dispose()


def table_names(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


class TestPostgresUpgrade:
    """Baseline: the chain must apply cleanly on the production database."""

    def test_upgrade_to_head_succeeds(self, clean_pg):
        result = run_alembic("upgrade", "head", database_url=clean_pg)
        assert result.returncode == 0, f"upgrade failed:\n{result.stderr}"
        tables = table_names(clean_pg)
        assert "alembic_version" in tables
        for expected in ("users", "workflows", "workflow_executions", "audit_events"):
            assert expected in tables, f"{expected} missing after upgrade"

    def test_enum_types_are_created(self, clean_pg):
        run_alembic("upgrade", "head", database_url=clean_pg)
        assert set(SCHEMA_ENUM_TYPES).issubset(enum_types(clean_pg))


class TestPostgresDowngradeCleansUpEnumTypes:
    """The actual M6-F3 regression."""

    def test_downgrade_to_base_leaves_no_enum_types(self, clean_pg):
        run_alembic("upgrade", "head", database_url=clean_pg)
        result = run_alembic("downgrade", "base", database_url=clean_pg)
        assert result.returncode == 0, f"downgrade failed:\n{result.stderr}"

        leftover = enum_types(clean_pg)
        assert leftover == set(), (
            "downgrade left orphaned PostgreSQL enum types behind, which makes "
            f"the next upgrade fail with DuplicateObject: {sorted(leftover)}"
        )

    def test_downgrade_to_base_drops_every_table_except_the_audit_log(
        self, clean_pg
    ):
        """``audit_events`` is deliberately retained; everything else goes.

        The M5 migration keeps the audit table on downgrade because it predates
        that revision as a model, and dropping it would destroy audit history
        that existed before M5. That is a defensible choice, and the upgrade
        guards with ``if not _has_table(...)`` so it stays idempotent — proven
        by the round-trip tests below.

        This test was originally written asserting an empty schema and failed;
        investigation showed the behaviour is intentional rather than a defect,
        so the assertion now pins the documented intent instead.
        """
        run_alembic("upgrade", "head", database_url=clean_pg)
        run_alembic("downgrade", "base", database_url=clean_pg)
        remaining = table_names(clean_pg) - {"alembic_version"}
        assert remaining == {"audit_events"}, (
            "downgrade should retain only the pre-existing audit log, "
            f"but left: {sorted(remaining)}"
        )


class TestPostgresRoundTrip:
    """The documented rollback-then-retry procedure must actually work."""

    def test_upgrade_downgrade_upgrade_succeeds(self, clean_pg):
        """Pre-M6 the second upgrade died on 'type executionstatus already exists'."""
        assert run_alembic("upgrade", "head", database_url=clean_pg).returncode == 0
        assert run_alembic("downgrade", "base", database_url=clean_pg).returncode == 0

        result = run_alembic("upgrade", "head", database_url=clean_pg)
        assert result.returncode == 0, (
            "re-upgrade after a full downgrade failed — this is the M6-F3 "
            f"regression:\n{result.stderr}"
        )
        assert "users" in table_names(clean_pg)

    def test_repeated_round_trips_are_stable(self, clean_pg):
        """Three cycles: a leak would compound and surface here."""
        for cycle in range(3):
            up = run_alembic("upgrade", "head", database_url=clean_pg)
            assert up.returncode == 0, f"cycle {cycle} upgrade failed:\n{up.stderr}"
            down = run_alembic("downgrade", "base", database_url=clean_pg)
            assert down.returncode == 0, f"cycle {cycle} downgrade failed:\n{down.stderr}"
            assert enum_types(clean_pg) == set(), f"cycle {cycle} leaked enum types"

    def test_single_step_rollback_and_retry(self, clean_pg):
        """`alembic downgrade -1` is the exact command DEPLOYMENT.md documents."""
        assert run_alembic("upgrade", "head", database_url=clean_pg).returncode == 0
        assert run_alembic("downgrade", "-1", database_url=clean_pg).returncode == 0
        result = run_alembic("upgrade", "head", database_url=clean_pg)
        assert result.returncode == 0, f"rollback/retry failed:\n{result.stderr}"


class TestOrmSchemaMatchesMigrations:
    """A model with no migration is invisible until it fails in production."""

    def test_every_orm_table_exists_after_upgrade(self, clean_pg):
        run_alembic("upgrade", "head", database_url=clean_pg)

        import app.domain.models  # noqa: F401  register every model
        from app.infrastructure.database.database import Base

        missing = set(Base.metadata.tables) - table_names(clean_pg)
        assert missing == set(), f"ORM tables with no migration: {sorted(missing)}"
