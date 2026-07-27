"""Database engine, session factory and FastAPI dependency.

Backwards compatible with V1.0 (``engine``, ``SessionLocal``, ``Base``,
``get_db`` all keep their names and semantics).

V1.1 additions:
- SQLite PRAGMA tuning: WAL journaling, foreign key enforcement, busy timeout,
  and a larger page cache. WAL alone materially improves concurrent read
  throughput for the desktop workload.
- Proper pooling configuration for non-SQLite backends (e.g. PostgreSQL).
- ``get_db`` now rolls back on exception instead of leaking a dirty session.
- ``session_scope()`` context manager for service-layer transactions.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from app.infrastructure.config.settings import settings


def _engine_kwargs() -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"echo": settings.DB_ECHO, "future": True}
    if settings.is_sqlite:
        kwargs["connect_args"] = {
            "check_same_thread": False,
            "timeout": settings.SQLITE_BUSY_TIMEOUT_MS / 1000,
        }
        if ":memory:" in settings.DATABASE_URL:
            kwargs["poolclass"] = StaticPool
    else:
        # M6-F6: the pool must be able to serve the number of requests the
        # ASGI threadpool will admit concurrently. When it cannot, every
        # excess request blocks on checkout for pool_timeout seconds and the
        # server appears hung rather than busy. pool_timeout is set explicitly
        # (SQLAlchemy defaults to 30s) so overload sheds fast instead.
        kwargs.update(
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
            pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
            pool_pre_ping=True,
        )
    return kwargs


engine: Engine = create_engine(settings.DATABASE_URL, **_engine_kwargs())


def apply_sqlite_pragmas(dbapi_connection, _connection_record=None) -> None:
    """Apply durability/concurrency PRAGMAs to a new SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={settings.SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA cache_size=-16000")  # ~16MB page cache
        cursor.execute("PRAGMA temp_store=MEMORY")
    except Exception:  # pragma: no cover - exotic SQLite builds
        pass
    finally:
        cursor.close()


if settings.is_sqlite:
    event.listen(engine, "connect", apply_sqlite_pragmas)


SessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
)

Base = declarative_base()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session that rolls back on failure."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create any tables that do not yet exist.

    Alembic remains the source of truth for schema migrations; this is a
    convenience for fresh local installs and tests.
    """
    import app.domain.models  # noqa: F401  (ensures model modules are imported)

    Base.metadata.create_all(bind=engine)


def dispose_engine() -> None:
    """Release pooled connections (called on application shutdown)."""
    engine.dispose()
