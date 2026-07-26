"""Structured, rotating, request-correlated logging for Creator OS.

Backwards compatible with V1.0: ``setup_logging()`` still returns the
``creator_os`` logger and the module-level ``logger`` singleton is preserved.

New in V1.1:
- optional JSON formatting (``LOG_FORMAT=json``) for machine ingestion
- rotating file handler (``LOG_FILE``) with size caps
- automatic ``request_id`` correlation via a contextvar
- secret redaction so API keys never reach disk
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

LOGGER_NAME = "creator_os"

# Correlates every log line emitted while handling one HTTP request.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)

_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)([^\s\",}]+)"),
    re.compile(r"(?i)(authorization\"?\s*[:=]\s*\"?)(bearer\s+)?([^\s\",}]+)"),
    re.compile(r"(?i)(password\"?\s*[:=]\s*\"?)([^\s\",}]+)"),
    re.compile(r"(?i)(token\"?\s*[:=]\s*\"?)([^\s\",}]+)"),
]


def redact(text: str) -> str:
    """Mask credential-looking substrings in a log message."""
    if not text:
        return text
    out = text
    out = _SECRET_PATTERNS[0].sub("sk-***REDACTED***", out)
    for pattern in _SECRET_PATTERNS[1:]:
        out = pattern.sub(lambda m: f"{m.group(1)}***REDACTED***", out)
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get("")
        return True


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line."""

    _RESERVED = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        rid = getattr(record, "request_id", "") or request_id_var.get("")
        if rid:
            payload["request_id"] = rid
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and key not in payload and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = str(value)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    DEFAULT_FMT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    def __init__(self) -> None:
        super().__init__(self.DEFAULT_FMT)

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        rid = getattr(record, "request_id", "") or request_id_var.get("")
        if rid:
            base = f"{base} [request_id={rid}]"
        return redact(base)


def setup_logging(
    level: Optional[str] = None,
    fmt: Optional[str] = None,
    log_file: Optional[str] = None,
    force: bool = False,
) -> logging.Logger:
    """Configure and return the root Creator OS logger.

    Args:
        level: ``DEBUG``/``INFO``/... Defaults to ``$LOG_LEVEL`` or ``INFO``.
        fmt: ``json`` or ``console``. Defaults to ``$LOG_FORMAT`` or ``console``.
        log_file: Path for a rotating file handler. Defaults to ``$LOG_FILE``.
        force: Rebuild handlers even if already configured.
    """
    level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    fmt = (fmt or os.getenv("LOG_FORMAT", "console")).lower()
    log_file = log_file if log_file is not None else os.getenv("LOG_FILE", "")

    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(getattr(logging, level, logging.INFO))
    log.propagate = False

    if log.handlers and not force:
        return log

    for handler in list(log.handlers):
        log.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - defensive
            pass

    formatter: logging.Formatter = (
        JsonFormatter() if fmt == "json" else ConsoleFormatter()
    )
    redactor = RedactingFilter()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redactor)
    log.addHandler(console_handler)

    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redactor)
            log.addHandler(file_handler)
        except OSError as exc:  # pragma: no cover - environment dependent
            log.warning("Could not open log file %s: %s", log_file, exc)

    return log


def get_logger(name: str = "") -> logging.Logger:
    """Return a namespaced child logger (``creator_os.<name>``)."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


logger = setup_logging()
