"""Database abstraction layer and domain DatabaseService.

SQLite is the v1.0 backend. Domain methods hide SQL from modules.
All writes are serialized with a threading lock; WAL mode is enabled.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from core.safe_io import replace_atomic, safe_unlink
from core.time_helper import utc_now_str

logger = logging.getLogger("autopilot.database")

Params = Union[Tuple[Any, ...], Dict[str, Any], Sequence[Any]]

# PHASE 9 (database safety): SQLite serializes writers, so a long render
# writing word timestamps while the UI reads progress can transiently
# collide. The connection timeout already waits, but a write that still
# loses is retried briefly rather than surfacing as a lost DB row.
_BUSY_RETRIES = 3
_BUSY_BACKOFF_SECONDS = 0.15

PRODUCT_TABLES: Tuple[str, ...] = (
    "projects",
    "scenes",
    "dialogue_lines",
    "voice_profiles",
    "channel_profiles",
    "render_progress",
    "render_history",
    "timeline_data",
    "audio_tracks",
    "subtitle_data",
    "word_timestamps",
    "sfx_placements",
    "image_assets",
    "installed_voices",
    "cloned_voices",
    "batch_queue",
    "quality_check_results",
    "license_data",
    "app_settings",
    "app_logs",
    "render_log_entries",
    "thumbnails",
    "recent_projects",
    "voice_store_cache",
    "engine_installations",
)


class DatabaseInterface(ABC):
    """Abstract base class defining the database interface."""

    @abstractmethod
    def initialize(self) -> bool:
        """Create all tables and indexes if they do not exist."""

    @abstractmethod
    def execute(self, sql: str, params: Params = ()) -> Any:
        """Execute a write query (INSERT, UPDATE, DELETE)."""

    @abstractmethod
    def execute_many(self, sql: str, params_seq: Sequence[Params]) -> Any:
        """Execute a write query across multiple parameter sets."""

    @abstractmethod
    def fetch_one(self, sql: str, params: Params = ()) -> Optional[Dict[str, Any]]:
        """Execute a SELECT and return a single row as dict."""

    @abstractmethod
    def fetch_all(self, sql: str, params: Params = ()) -> List[Dict[str, Any]]:
        """Execute a SELECT and return all rows as list of dicts."""

    @abstractmethod
    def begin_transaction(self) -> None:
        """Start a database transaction."""

    @abstractmethod
    def commit_transaction(self) -> None:
        """Commit current transaction."""

    @abstractmethod
    def rollback_transaction(self) -> None:
        """Rollback current transaction on error."""

    @abstractmethod
    def backup(self, backup_path: str) -> bool:
        """Create a backup of the database."""

    @abstractmethod
    def integrity_check(self) -> bool:
        """Run integrity check and return True if database is healthy."""

    @abstractmethod
    def get_table_row_count(self, table_name: str) -> int:
        """Return number of rows in specified table."""


class SQLiteDatabase(DatabaseInterface):
    """SQLite implementation of DatabaseInterface."""

    def __init__(
        self, db_path: str | Path, schema_path: str | Path | None = None
    ) -> None:
        """Create SQLite backend.

        Args:
            db_path: Path to the .db file.
            schema_path: Path to schema.sql (default: database/schema.sql).
        """
        self.db_path = Path(db_path)
        self.schema_path = (
            Path(schema_path) if schema_path else Path("database/schema.sql")
        )
        self._lock = threading.RLock()
        self._local = threading.local()
        # PHASE 9: every connection ever handed out, so close_all() can
        # release the ones created on worker threads (a render pool
        # thread that exits leaves its connection — and its WAL handle —
        # open until interpreter shutdown).
        self._connections: List[sqlite3.Connection] = []
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Cannot create database folder %s: %s", self.db_path, exc)
        logger.info("SQLite database path: %s", self.db_path)

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection with PRAGMAs applied.

        PHASE 9: an individual PRAGMA can fail on an unusual filesystem
        (WAL is unsupported on some network shares) — that is a reason
        to run with the default journal mode, not to fail every database
        call for the rest of the session. Each PRAGMA is therefore
        applied best-effort while the connection itself still surfaces
        real errors (a missing/locked database file).
        """
        if not hasattr(self._local, "connection") or self._local.connection is None:
            conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=30.0,
            )
            conn.row_factory = sqlite3.Row
            for pragma in (
                "PRAGMA foreign_keys = ON",
                "PRAGMA journal_mode = WAL",
                "PRAGMA synchronous = NORMAL",
                "PRAGMA cache_size = 10000",
                "PRAGMA temp_store = MEMORY",
            ):
                try:
                    conn.execute(pragma)
                except sqlite3.Error as exc:
                    logger.warning("Could not apply %s: %s", pragma, exc)
            self._local.connection = conn
            with self._lock:
                self._connections.append(conn)
        return self._local.connection  # type: ignore[no-any-return]

    def _discard_connection(self) -> None:
        """Drop a connection that raised, so the next call reconnects.

        PHASE 9: once a connection is broken (the database file was
        replaced, a disk error occurred) every later statement on it
        fails too. Discarding it lets the very next call rebuild a
        healthy connection instead of failing for the rest of the run.
        """
        conn = getattr(self._local, "connection", None)
        self._local.connection = None
        if conn is None:
            return
        with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)
        try:
            conn.close()
        except sqlite3.Error:
            pass

    def initialize(self) -> bool:
        """Apply schema.sql to create tables, indexes, and defaults."""
        try:
            if not self.schema_path.exists():
                logger.error("Schema file not found: %s", self.schema_path)
                return False
            schema_sql = self.schema_path.read_text(encoding="utf-8")
            with self._lock:
                conn = self._get_connection()
                conn.executescript(schema_sql)
                conn.commit()
            logger.info("Database initialized successfully")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Database initialization failed: %s", exc)
            return False

    @property
    def _in_transaction(self) -> bool:
        return getattr(self._local, "in_transaction", 0) > 0

    def execute(self, sql: str, params: Params = ()) -> Any:
        """Execute a write statement under the global write lock.

        PHASE 9: a transient "database is locked" (another process or a
        checkpoint holding the write lock past the connection timeout)
        is retried with a short backoff before giving up. Real errors
        (constraint violations, syntax) still raise immediately and
        unchanged, so callers' error handling is untouched.
        PHASE 10: avoid unnecessary commits inside explicit transactions.
        """
        with self._lock:
            last_error: Optional[Exception] = None
            for attempt in range(_BUSY_RETRIES):
                conn = self._get_connection()
                try:
                    cursor = conn.execute(sql, params)
                    if not self._in_transaction:
                        conn.commit()
                    return cursor
                except sqlite3.OperationalError as exc:
                    self._safe_rollback(conn)
                    if not self._in_transaction:
                        self._local.in_transaction = 0
                    if "locked" not in str(exc).lower() and "busy" not in str(
                        exc
                    ).lower():
                        logger.error(
                            "Database execute error: %s | SQL: %s", exc, sql[:120]
                        )
                        raise
                    last_error = exc
                    logger.warning(
                        "Database busy (attempt %d/%d): %s",
                        attempt + 1,
                        _BUSY_RETRIES,
                        exc,
                    )
                    time.sleep(_BUSY_BACKOFF_SECONDS * (attempt + 1))
                except Exception as exc:
                    self._safe_rollback(conn)
                    if not self._in_transaction:
                        self._local.in_transaction = 0
                    logger.error("Database execute error: %s | SQL: %s", exc, sql[:120])
                    raise
            logger.error(
                "Database execute failed after %d attempts | SQL: %s",
                _BUSY_RETRIES,
                sql[:120],
            )
            raise last_error if last_error is not None else RuntimeError(
                "database execute failed"
            )

    def execute_many(self, sql: str, params_seq: Sequence[Params]) -> Any:
        """Execute a write statement across multiple parameter sets in one transaction."""
        if not params_seq:
            return None
        with self._lock:
            last_error: Optional[Exception] = None
            for attempt in range(_BUSY_RETRIES):
                conn = self._get_connection()
                try:
                    in_tx = self._in_transaction
                    if not in_tx:
                        conn.execute("BEGIN")
                    cursor = conn.executemany(sql, params_seq)
                    if not in_tx:
                        conn.commit()
                    return cursor
                except sqlite3.OperationalError as exc:
                    self._safe_rollback(conn)
                    if not self._in_transaction:
                        self._local.in_transaction = 0
                    if "locked" not in str(exc).lower() and "busy" not in str(
                        exc
                    ).lower():
                        logger.error(
                            "Database executemany error: %s | SQL: %s", exc, sql[:120]
                        )
                        raise
                    last_error = exc
                    logger.warning(
                        "Database busy (attempt %d/%d): %s",
                        attempt + 1,
                        _BUSY_RETRIES,
                        exc,
                    )
                    time.sleep(_BUSY_BACKOFF_SECONDS * (attempt + 1))
                except Exception as exc:
                    self._safe_rollback(conn)
                    if not self._in_transaction:
                        self._local.in_transaction = 0
                    logger.error(
                        "Database executemany error: %s | SQL: %s", exc, sql[:120]
                    )
                    raise
            logger.error(
                "Database executemany failed after %d attempts | SQL: %s",
                _BUSY_RETRIES,
                sql[:120],
            )
            raise last_error if last_error is not None else RuntimeError(
                "database executemany failed"
            )

    @staticmethod
    def _safe_rollback(conn: sqlite3.Connection) -> None:
        """Roll back without masking the error that triggered it."""
        try:
            conn.rollback()
        except sqlite3.Error as exc:
            logger.warning("Rollback failed: %s", exc)

    def fetch_one(self, sql: str, params: Params = ()) -> Optional[Dict[str, Any]]:
        """Fetch a single row as a dict."""
        try:
            conn = self._get_connection()
            cursor = conn.execute(sql, params)
            row = cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as exc:
            logger.error("Database fetch_one error: %s | SQL: %s", exc, sql[:120])
            self._discard_connection()
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error("Database fetch_one error: %s", exc)
            return None

    def fetch_all(self, sql: str, params: Params = ()) -> List[Dict[str, Any]]:
        """Fetch all rows as dicts."""
        try:
            conn = self._get_connection()
            cursor = conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            logger.error("Database fetch_all error: %s | SQL: %s", exc, sql[:120])
            self._discard_connection()
            return []
        except Exception as exc:  # noqa: BLE001
            logger.error("Database fetch_all error: %s", exc)
            return []

    def begin_transaction(self) -> None:
        """Begin an explicit transaction."""
        with self._lock:
            conn = self._get_connection()
            if not self._in_transaction:
                conn.execute("BEGIN")
            self._local.in_transaction = getattr(self._local, "in_transaction", 0) + 1

    def commit_transaction(self) -> None:
        """Commit the current transaction."""
        with self._lock:
            depth = getattr(self._local, "in_transaction", 0)
            if depth > 1:
                self._local.in_transaction = depth - 1
            else:
                self._local.in_transaction = 0
                conn = self._get_connection()
                conn.commit()

    def rollback_transaction(self) -> None:
        """Roll back the current transaction."""
        with self._lock:
            self._local.in_transaction = 0
            conn = self._get_connection()
            conn.rollback()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block as one transaction, rolling back on any error.

        PHASE 9 (prevent partial project corruption): multi-statement
        writes (persisting a whole script's scenes and dialogue lines,
        for instance) previously auto-committed each statement, so a
        failure half-way left a project row with only some of its
        children. Callers that need all-or-nothing now wrap the block::

            with db.transaction():
                ...

        Nothing existing changes: single ``execute`` calls keep their
        own commit-per-statement behavior.
        """
        with self._lock:
            conn = self._get_connection()
            already_in = self._in_transaction
            if not already_in:
                try:
                    conn.execute("BEGIN")
                except sqlite3.Error as exc:
                    logger.error("Could not begin transaction: %s", exc)
                    raise
            self._local.in_transaction = getattr(self._local, "in_transaction", 0) + 1
            try:
                yield conn
            except BaseException:
                self._safe_rollback(conn)
                self._local.in_transaction = 0
                raise
            else:
                depth = getattr(self._local, "in_transaction", 0)
                if depth > 1:
                    self._local.in_transaction = depth - 1
                else:
                    self._local.in_transaction = 0
                    try:
                        conn.commit()
                    except sqlite3.Error as exc:
                        logger.error("Transaction commit failed: %s", exc)
                        self._safe_rollback(conn)
                        raise

    def backup(self, backup_path: str) -> bool:
        """Copy the database to backup_path using SQLite backup API.

        PHASE 9: the backup is built at a temp path and renamed into
        place, so an interrupted backup can never overwrite a good
        previous backup with a truncated file.
        """
        target = Path(backup_path)
        temp = target.with_name(f"{target.name}.partial")
        try:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.error("Cannot create backup folder for %s: %s", target, exc)
                return False
            with self._lock:
                source = self._get_connection()
                backup_conn = sqlite3.connect(str(temp))
                try:
                    source.backup(backup_conn)
                finally:
                    backup_conn.close()
            replace_atomic(temp, target)
            logger.info("Database backed up to: %s", backup_path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Database backup failed: %s", exc)
            safe_unlink(temp)
            return False

    def integrity_check(self) -> bool:
        """Run PRAGMA integrity_check."""
        try:
            result = self.fetch_one("PRAGMA integrity_check")
            is_ok = bool(result and list(result.values())[0] == "ok")
            if is_ok:
                logger.info("Database integrity check passed")
            else:
                logger.error("Database integrity check failed: %s", result)
            return is_ok
        except Exception as exc:  # noqa: BLE001
            logger.error("Integrity check error: %s", exc)
            return False

    def get_table_row_count(self, table_name: str) -> int:
        """Return row count for a table (name must be known product table)."""
        if table_name not in PRODUCT_TABLES and table_name != "schema_migrations":
            logger.error("Refusing row count for unknown table: %s", table_name)
            return 0
        result = self.fetch_one(f'SELECT COUNT(*) AS count FROM "{table_name}"')
        return int(result["count"]) if result else 0

    def vacuum(self) -> bool:
        """Compact database to reclaim disk space."""
        try:
            with self._lock:
                conn = self._get_connection()
                conn.execute("VACUUM")
            logger.info("Database vacuumed successfully")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Database vacuum failed: %s", exc)
            return False

    def get_database_size_mb(self) -> float:
        """Return database file size in megabytes."""
        try:
            return self.db_path.stat().st_size / 1024 / 1024
        except OSError:
            return 0.0

    def list_tables(self) -> List[str]:
        """Return user table names (excluding sqlite internal tables)."""
        rows = self.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [row["name"] for row in rows]

    def close(self) -> None:
        """Close the thread-local connection if open."""
        conn = getattr(self._local, "connection", None)
        if conn is None:
            return
        with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)
        try:
            conn.close()
        except sqlite3.Error as exc:  # PHASE 9: closing must never raise
            logger.warning("Database close failed: %s", exc)
        self._local.connection = None

    def close_all(self) -> int:
        """Close every connection this backend opened, on any thread.

        PHASE 9 (resource/thread cleanup): render pools create
        thread-local connections that outlive their worker thread and
        keep WAL/shm handles open — on Windows that also blocks deleting
        or moving the project database. Called at shutdown; safe to call
        more than once.

        Returns:
            Number of connections closed.
        """
        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        closed = 0
        for conn in connections:
            try:
                conn.close()
                closed += 1
            except sqlite3.Error as exc:
                logger.warning("Database close failed: %s", exc)
        self._local.connection = None
        return closed


class DatabaseService:
    """High-level service wrapping DatabaseInterface for modules."""

    def __init__(self, database: DatabaseInterface) -> None:
        """Inject a database backend.

        Args:
            database: Concrete DatabaseInterface implementation.
        """
        self.db = database
        self.logger = logging.getLogger("autopilot.database_service")

    def initialize(self) -> bool:
        """Initialize underlying database schema."""
        return self.db.initialize()

    def execute_many(self, sql: str, params_seq: Sequence[Params]) -> Any:
        """Proxy execute_many to underlying database."""
        return self.db.execute_many(sql, params_seq)

    def integrity_check(self) -> bool:
        """Proxy integrity check."""
        return self.db.integrity_check()

    def list_product_tables(self) -> List[str]:
        """Return product table names present in the database."""
        existing = (
            set(self.db.list_tables()) if hasattr(self.db, "list_tables") else set()
        )
        return [name for name in PRODUCT_TABLES if name in existing]

    def verify_product_tables(self) -> Tuple[bool, List[str]]:
        """Verify all 25 product tables exist.

        Returns:
            (ok, missing_table_names)
        """
        existing = (
            set(self.db.list_tables()) if hasattr(self.db, "list_tables") else set()
        )
        missing = [name for name in PRODUCT_TABLES if name not in existing]
        return (len(missing) == 0, missing)

    def create_project(self, project_data: Dict[str, Any]) -> bool:
        """Insert a project row.

        Args:
            project_data: Dict with id, title, channel_profile_id, genre,
                status, created_at, updated_at, project_folder_path.

        Returns:
            True on success.
        """
        sql = """
        INSERT INTO projects (
            id, title, channel_profile_id, genre, status,
            created_at, updated_at, project_folder_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            self.db.execute(
                sql,
                (
                    project_data["id"],
                    project_data["title"],
                    project_data.get("channel_profile_id", "default"),
                    project_data.get("genre", "dark_history"),
                    project_data.get("status", "new"),
                    project_data.get("created_at", utc_now_str()),
                    project_data.get("updated_at", utc_now_str()),
                    project_data["project_folder_path"],
                ),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.error("create_project failed: %s", exc)
            return False

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a project by ID."""
        return self.db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))

    def update_project_status(self, project_id: str, status: str) -> bool:
        """Update project status and updated_at."""
        sql = "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?"
        try:
            self.db.execute(sql, (status, utc_now_str(), project_id))
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.error("update_project_status failed: %s", exc)
            return False

    def save_scene(self, scene_data: Dict[str, Any]) -> bool:
        """Insert or replace a scene row."""
        sql = """
        INSERT OR REPLACE INTO scenes (
            id, project_id, scene_number, image_filename, image_file_path,
            start_time, end_time, duration, transition_in, transition_out,
            animation_type, color_grade_override, sfx_trigger, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            self.db.execute(
                sql,
                (
                    scene_data["id"],
                    scene_data["project_id"],
                    scene_data["scene_number"],
                    scene_data.get("image_filename"),
                    scene_data.get("image_file_path"),
                    scene_data.get("start_time", 0.0),
                    scene_data.get("end_time", 0.0),
                    scene_data.get("duration", 0.0),
                    scene_data.get("transition_in", "crossfade"),
                    scene_data.get("transition_out", "crossfade"),
                    scene_data.get("animation_type", "ken_burns"),
                    scene_data.get("color_grade_override"),
                    scene_data.get("sfx_trigger"),
                    scene_data.get("status", "pending"),
                    scene_data.get("created_at", utc_now_str()),
                    scene_data.get("updated_at", utc_now_str()),
                ),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.error("save_scene failed: %s", exc)
            return False

    def get_all_scenes(self, project_id: str) -> List[Dict[str, Any]]:
        """Return scenes for a project ordered by scene_number."""
        return self.db.fetch_all(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY scene_number ASC",
            (project_id,),
        )

    def save_render_progress(self, progress_data: Dict[str, Any]) -> bool:
        """Insert or replace resumable render progress."""
        sql = """
        INSERT OR REPLACE INTO render_progress (
            id, project_id, render_session_id, current_stage, stage_percent,
            current_scene_id, current_scene_number, total_scenes,
            completed_scenes_json, tts_completed_lines, segment_files_json,
            started_at, updated_at, render_settings_json, is_resumable
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            self.db.execute(
                sql,
                (
                    progress_data["id"],
                    progress_data["project_id"],
                    progress_data["render_session_id"],
                    progress_data["current_stage"],
                    progress_data.get("stage_percent", 0.0),
                    progress_data.get("current_scene_id"),
                    progress_data.get("current_scene_number", 0),
                    progress_data.get("total_scenes", 0),
                    progress_data.get("completed_scenes_json", "[]"),
                    progress_data.get("tts_completed_lines", "[]"),
                    progress_data.get("segment_files_json", "[]"),
                    progress_data.get("started_at", utc_now_str()),
                    progress_data.get("updated_at", utc_now_str()),
                    progress_data.get("render_settings_json", "{}"),
                    progress_data.get("is_resumable", 1),
                ),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.error("save_render_progress failed: %s", exc)
            return False

    def get_render_progress(self, project_id: str) -> Optional[Dict[str, Any]]:
        """Fetch resumable render progress for a project."""
        return self.db.fetch_one(
            "SELECT * FROM render_progress WHERE project_id = ? AND is_resumable = 1",
            (project_id,),
        )

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Read an app_settings value by key."""
        row = self.db.fetch_one("SELECT value FROM app_settings WHERE key = ?", (key,))
        if row is None:
            return default
        return str(row["value"])

    def set_setting(
        self,
        key: str,
        value: str,
        value_type: str = "string",
        category: str = "general",
    ) -> bool:
        """Upsert an app_settings row."""
        sql = """
        INSERT INTO app_settings (key, value, value_type, category, description, updated_at)
        VALUES (?, ?, ?, ?, '', ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            value_type = excluded.value_type,
            updated_at = excluded.updated_at
        """
        try:
            self.db.execute(sql, (key, value, value_type, category, utc_now_str()))
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.error("set_setting failed: %s", exc)
            return False

    def log_event(
        self,
        level: str,
        module: str,
        message: str,
        project_id: Optional[str] = None,
    ) -> None:
        """Store a log entry in app_logs."""
        sql = """
        INSERT INTO app_logs (timestamp, level, module, message, project_id)
        VALUES (?, ?, ?, ?, ?)
        """
        try:
            self.db.execute(sql, (utc_now_str(), level, module, message, project_id))
        except Exception:  # noqa: BLE001
            pass

    def cleanup_old_logs(self, max_rows: int = 10000) -> int:
        """Delete oldest log entries if count exceeds max_rows."""
        count = self.db.get_table_row_count("app_logs")
        if count <= max_rows:
            return 0
        rows_to_delete = count - max_rows
        self.db.execute(
            "DELETE FROM app_logs WHERE id IN "
            "(SELECT id FROM app_logs ORDER BY timestamp ASC LIMIT ?)",
            (rows_to_delete,),
        )
        return rows_to_delete

    def get_statistics(self) -> Dict[str, Any]:
        """Return database statistics for monitoring."""
        size_mb = 0.0
        if hasattr(self.db, "get_database_size_mb"):
            size_mb = float(self.db.get_database_size_mb())  # type: ignore[attr-defined]
        return {
            "total_projects": self.db.get_table_row_count("projects"),
            "total_scenes": self.db.get_table_row_count("scenes"),
            "total_dialogue_lines": self.db.get_table_row_count("dialogue_lines"),
            "total_word_timestamps": self.db.get_table_row_count("word_timestamps"),
            "total_renders": self.db.get_table_row_count("render_history"),
            "total_logs": self.db.get_table_row_count("app_logs"),
            "database_size_mb": size_mb,
        }

    @staticmethod
    def new_id() -> str:
        """Generate a new UUID4 string primary key."""
        return str(uuid.uuid4())
