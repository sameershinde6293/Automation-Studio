"""Structured logging service for Autopilot.

Creates rotating file handlers under the logs folder and attaches a
correlation-ID filter so render sessions are easy to trace.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

from core.correlation import CorrelationFilter
from core.safe_io import writable_directory


class LogService:
    """Sets up structured logging with separate module log files."""

    DEFAULT_FORMAT = (
        "%(asctime)s | %(levelname)-8s | %(correlation_prefix)s%(name)s | %(message)s"
    )
    DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    def __init__(
        self,
        log_folder: str | Path = "logs",
        level: str = "INFO",
        max_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 5,
        console: bool = True,
    ) -> None:
        """Initialize logging service.

        Args:
            log_folder: Directory for log files.
            level: Root log level name.
            max_bytes: Max size per log file before rotation.
            backup_count: Number of rotated files to keep.
            console: Whether to also log to stderr.
        """
        # PHASE 9: logging must never be the reason the app won't start.
        # A read-only install folder or a removed network drive falls
        # back to the OS temp area instead of raising out of boot.
        self.log_folder = writable_directory(log_folder, "autopilot-logs")
        self.level = getattr(logging, level.upper(), logging.INFO)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.console = console
        self._configured = False
        self._correlation_filter = CorrelationFilter()
        self.setup()

    def _make_rotating_handler(
        self,
        filename: str,
        formatter: logging.Formatter,
        level: Optional[int] = None,
    ) -> logging.Handler:
        """Create a rotating file handler with correlation filter."""
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            self.log_folder / filename,
            maxBytes=self.max_bytes,
            backupCount=self.backup_count,
            encoding="utf-8",
        )
        if level is not None:
            handler.setLevel(level)
        handler.setFormatter(formatter)
        handler.addFilter(self._correlation_filter)
        return handler

    def _install_record_factory(self) -> None:
        """Ensure log records always expose correlation_prefix."""
        old_factory = logging.getLogRecordFactory()

        def record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
            record = old_factory(*args, **kwargs)
            if not hasattr(record, "correlation_prefix"):
                record.correlation_prefix = ""  # type: ignore[attr-defined]
            return record

        logging.setLogRecordFactory(record_factory)

    def setup(self) -> None:
        """Configure root autopilot logger and handlers."""
        if self._configured:
            return

        root = logging.getLogger("autopilot")
        root.setLevel(self.level)
        root.handlers.clear()
        root.propagate = False

        formatter = logging.Formatter(self.DEFAULT_FORMAT, datefmt=self.DATE_FORMAT)
        # PHASE 9: a file handler can still fail after the folder check
        # (the file itself is locked by another Autopilot instance, or
        # the volume filled between the two). Console logging is then
        # the only sink — degraded, but the app runs.
        for filename, handler_level in (
            ("autopilot.log", None),
            ("errors.log", logging.ERROR),
        ):
            try:
                root.addHandler(
                    self._make_rotating_handler(filename, formatter, handler_level)
                )
            except OSError as exc:
                print(f"[log] cannot open {filename} ({exc}); console only")
        if self.console:
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setFormatter(formatter)
            console_handler.addFilter(self._correlation_filter)
            root.addHandler(console_handler)

        self._install_record_factory()
        self._configured = True
        root.info("LogService initialized at %s", self.log_folder)

    def get_logger(self, name: str) -> logging.Logger:
        """Return a child logger under the autopilot namespace.

        Args:
            name: Module or component name.

        Returns:
            Configured logger instance.
        """
        if not name.startswith("autopilot"):
            name = f"autopilot.{name}"
        return logging.getLogger(name)

    def set_level(self, level: str) -> None:
        """Change root log level at runtime.

        Args:
            level: Level name (DEBUG, INFO, WARNING, ERROR).
        """
        self.level = getattr(logging, level.upper(), logging.INFO)
        logging.getLogger("autopilot").setLevel(self.level)

    def get_log_folder(self) -> Path:
        """Return the log directory path.

        Returns:
            Path to logs folder.
        """
        return self.log_folder
