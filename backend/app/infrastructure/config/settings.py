"""Application settings.

All values are overridable via environment variables or a local ``.env`` file.
Backwards compatible with V1.0: ``APP_NAME``, ``VERSION``, ``ENVIRONMENT`` and
``DATABASE_URL`` keep their original names and defaults.

List-valued settings (M6)
-------------------------
``CORS_ORIGINS``, ``ALLOWED_HOSTS`` and friends accept **either** a JSON array
or a plain comma-separated string. Getting that to work from the environment
needs more than a ``field_validator``, and the M6 audit found that the M5 code
did not: see :class:`_ListFriendlyEnvSource` below.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import DotEnvSettingsSource, EnvSettingsSource

from app.version import __version__


def _decode_list_friendly(value: Any, original) -> Any:
    """JSON-decode ``value``, falling back to comma-separated parsing.

    ``pydantic-settings`` decodes complex (list/dict) fields *inside the
    settings source*, before any field validator runs, and raises
    ``SettingsError`` when the value is not valid JSON. Because ``Settings()``
    is constructed at module import, that error kills the process before
    logging or startup validation exist to report it.

    M6 found this the hard way: every documented production ``.env`` uses
    ``CORS_ORIGINS=https://a,https://b``, so a deployment that followed the
    documentation could not boot at all, and the M5 startup-validation gate —
    whose entire job is to refuse an unsafe production config — was
    unreachable. See docs/M6_VALIDATION_REPORT.md finding M6-F1.

    The fix is to widen the decoder rather than to narrow the documentation:
    JSON is still accepted (so nothing that worked before changes), and a bare
    comma-separated string now decodes to a list instead of raising.
    """
    if not isinstance(value, str):
        return original(value)
    stripped = value.strip()
    if not stripped:
        return []
    # Anything that looks like JSON is decoded as JSON, so a genuinely
    # malformed JSON array still reports a JSON error rather than being
    # silently mangled into a one-element list.
    if stripped[0] in "[{":
        return original(value)
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return [item.strip() for item in stripped.split(",") if item.strip()]


class _ListFriendlyEnvSource(EnvSettingsSource):
    """``EnvSettingsSource`` that accepts comma-separated lists."""

    def decode_complex_value(self, field_name, field, value):  # type: ignore[override]
        return _decode_list_friendly(
            value, lambda v: super(_ListFriendlyEnvSource, self).decode_complex_value(
                field_name, field, v
            )
        )


class _ListFriendlyDotEnvSource(DotEnvSettingsSource):
    """``DotEnvSettingsSource`` that accepts comma-separated lists."""

    def decode_complex_value(self, field_name, field, value):  # type: ignore[override]
        return _decode_list_friendly(
            value,
            lambda v: super(
                _ListFriendlyDotEnvSource, self
            ).decode_complex_value(field_name, field, v),
        )


class Settings(BaseSettings):
    # --- Identity -----------------------------------------------------------
    APP_NAME: str = "Creator OS Backend"
    VERSION: str = __version__
    ENVIRONMENT: str = "development"

    # --- Persistence --------------------------------------------------------
    DATABASE_URL: str = "sqlite:///./creator_os.db"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE_SECONDS: int = 1800
    SQLITE_BUSY_TIMEOUT_MS: int = 5000

    # --- Logging ------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "console"  # "console" | "json"
    LOG_FILE: str = ""

    # --- HTTP / security ----------------------------------------------------
    CORS_ORIGINS: List[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "app://.",
        "file://",
    ]
    MAX_REQUEST_BYTES: int = 25 * 1024 * 1024
    RATE_LIMIT_REQUESTS: int = 300
    RATE_LIMIT_WINDOW_SECONDS: float = 60.0
    RATE_LIMIT_ENABLED: bool = True
    ENABLE_DOCS: bool = True
    #: Hostnames accepted in the Host header. ["*"] disables the check.
    ALLOWED_HOSTS: List[str] = ["*"]
    #: Enable HSTS. Only meaningful when the API is served over TLS.
    SECURITY_HSTS_ENABLED: bool = False
    SECURITY_HSTS_MAX_AGE: int = 31536000
    #: Trust X-Forwarded-For for client identification (rate limiting, audit).
    #: Only enable behind a proxy you control, or clients can spoof their IP.
    TRUST_PROXY_HEADERS: bool = False
    #: Stricter rate limit for credential endpoints (per client, per window).
    AUTH_RATE_LIMIT_REQUESTS: int = 10
    AUTH_RATE_LIMIT_WINDOW_SECONDS: float = 60.0

    # --- M5: authentication and authorization -------------------------------
    #: Master switch. False = single-user desktop mode: every caller is treated
    #: as a local admin, preserving pre-M5 behaviour for existing clients.
    #: Startup validation refuses to run production with this off.
    AUTH_ENABLED: bool = False
    #: HMAC signing secret for JWTs. Required whenever AUTH_ENABLED is true.
    AUTH_SECRET_KEY: str = ""
    AUTH_ACCESS_TOKEN_TTL_SECONDS: float = 900.0  # 15 minutes
    AUTH_REFRESH_TOKEN_TTL_SECONDS: float = 1209600.0  # 14 days
    AUTH_TOKEN_ISSUER: str = "creator-os"
    AUTH_TOKEN_AUDIENCE: str = "creator-os-api"
    AUTH_API_KEY_HEADER: str = "X-API-Key"
    AUTH_MAX_FAILED_LOGINS: int = 5
    AUTH_LOCKOUT_SECONDS: float = 900.0
    #: Allow anonymous self-registration. Off by default: an internal
    #: automation platform should not let strangers create accounts.
    AUTH_ALLOW_SELF_REGISTRATION: bool = False
    #: First-run admin, created only when the user table is empty.
    AUTH_BOOTSTRAP_USERNAME: str = ""
    AUTH_BOOTSTRAP_PASSWORD: str = ""
    #: Require a CSRF token on cookie-authenticated unsafe requests.
    CSRF_PROTECTION_ENABLED: bool = True
    CSRF_HEADER_NAME: str = "X-CSRF-Token"
    CSRF_COOKIE_NAME: str = "creator_os_csrf"

    # --- M5: observability --------------------------------------------------
    #: Expose Prometheus text-format metrics at /metrics.
    METRICS_ENABLED: bool = True
    #: Restrict /metrics to callers holding manage_settings. Off by default so
    #: a scraper on a private network does not need a credential.
    METRICS_REQUIRE_AUTH: bool = False
    #: Ring-buffer size for the error aggregation endpoint.
    ERROR_AGGREGATION_SIZE: int = 500

    # --- Workflow engine ----------------------------------------------------
    WORKFLOW_MAX_PARALLEL_NODES: int = 8
    WORKFLOW_NODE_TIMEOUT_SECONDS: float = 300.0
    WORKFLOW_MAX_RETRIES: int = 3
    WORKFLOW_RETRY_BASE_DELAY: float = 1.0
    WORKFLOW_MAX_NODES: int = 1000

    # --- M4: execution queue, workers and limits ----------------------------
    #: Executions running concurrently. Additional runs wait in the priority queue.
    EXECUTION_MAX_WORKERS: int = 4
    #: Hard cap on queued (not yet running) executions; admission returns 429 beyond.
    EXECUTION_QUEUE_MAX_SIZE: int = 1000
    #: Whole-workflow wall-clock timeout.
    EXECUTION_TIMEOUT_SECONDS: float = 3600.0
    #: Poll interval used while an execution is paused.
    EXECUTION_PAUSE_POLL_SECONDS: float = 0.25
    #: Maximum iterations for a single loop node (guards runaway loops).
    WORKFLOW_MAX_LOOP_ITERATIONS: int = 1000
    #: Maximum total node executions per run, counting loop iterations.
    WORKFLOW_MAX_NODE_EXECUTIONS: int = 10000
    #: Log rows buffered in memory before a batched flush to the database.
    EXECUTION_LOG_BATCH_SIZE: int = 25
    #: Seconds between forced log flushes even when the batch is not full.
    EXECUTION_LOG_FLUSH_INTERVAL: float = 1.0
    #: Per-execution in-memory log ring buffer, used to backfill SSE clients.
    EXECUTION_LOG_BUFFER_SIZE: int = 500
    #: Bounded queue per SSE subscriber; a slow client is dropped, never blocks.
    EXECUTION_STREAM_QUEUE_SIZE: int = 256
    #: SSE keepalive comment interval.
    EXECUTION_STREAM_HEARTBEAT_SECONDS: float = 15.0
    #: Truncate any single node output persisted to the DB beyond this size.
    EXECUTION_MAX_OUTPUT_BYTES: int = 256 * 1024

    # --- M4/M5: script node executors (see docs/SECURITY.md for the honest
    # statement of what the M5 sandbox does and does not guarantee) ----------
    #: Python node. Disabled by default.
    ALLOW_PYTHON_EXECUTOR: bool = False
    PYTHON_EXECUTOR_TIMEOUT_SECONDS: float = 30.0
    #: M5: run script nodes in a separate OS process with POSIX resource
    #: limits (CPU, address space, file size, subprocess count) instead of an
    #: in-process restricted exec. Strongly recommended; the in-process path
    #: remains only as a fallback for platforms without fork+setrlimit.
    SCRIPT_SANDBOX_ENABLED: bool = True
    #: Hard CPU-seconds limit (RLIMIT_CPU). Kills busy loops that a wall-clock
    #: timeout alone cannot stop.
    SCRIPT_SANDBOX_CPU_SECONDS: int = 10
    #: Address-space limit in MB (RLIMIT_AS). Bounds runaway allocation.
    SCRIPT_SANDBOX_MEMORY_MB: int = 256
    #: Max bytes a sandboxed script may write to any file (RLIMIT_FSIZE).
    SCRIPT_SANDBOX_MAX_FILE_BYTES: int = 1024 * 1024
    #: Max stdout bytes captured from a sandboxed script.
    SCRIPT_SANDBOX_MAX_OUTPUT_BYTES: int = 256 * 1024
    #: Modules a sandboxed Python script may import. Everything else is denied.
    SCRIPT_SANDBOX_ALLOWED_MODULES: List[str] = [
        "json", "math", "re", "datetime", "random", "statistics",
        "itertools", "functools", "collections", "string", "base64",
        "hashlib", "uuid", "decimal", "textwrap",
    ]
    #: Block outbound network access from sandboxed scripts.
    SCRIPT_SANDBOX_BLOCK_NETWORK: bool = True
    #: Maximum script-node invocations within a single workflow execution.
    SCRIPT_EXECUTION_QUOTA_PER_RUN: int = 100
    #: JavaScript node requires a local Node.js binary. Disabled by default.
    ALLOW_JAVASCRIPT_EXECUTOR: bool = False
    JAVASCRIPT_BINARY: str = "node"
    JAVASCRIPT_EXECUTOR_TIMEOUT_SECONDS: float = 30.0
    #: Database node: raw SQL against the app database. Disabled by default.
    ALLOW_DATABASE_EXECUTOR: bool = False
    DATABASE_EXECUTOR_MAX_ROWS: int = 1000
    #: Email node: unset host = dry-run mode (renders but does not send).
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_FROM_ADDRESS: str = ""
    SMTP_TIMEOUT_SECONDS: float = 30.0

    # --- Node executor safety ----------------------------------------------
    ALLOW_SHELL_EXECUTOR: bool = False
    SHELL_ALLOWED_COMMANDS: List[str] = []
    SHELL_TIMEOUT_SECONDS: float = 30.0
    HTTP_EXECUTOR_TIMEOUT_SECONDS: float = 30.0
    HTTP_EXECUTOR_MAX_RESPONSE_BYTES: int = 5 * 1024 * 1024
    HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS: bool = False
    HTTP_EXECUTOR_ALLOWED_HOSTS: List[str] = []

    # --- AI runtime ---------------------------------------------------------
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OLLAMA_BASE_URL: str = "http://localhost:11434/api"
    AI_REQUEST_TIMEOUT_SECONDS: float = 120.0
    AI_MAX_RETRIES: int = 2
    AI_CONTEXT_MAX_MESSAGES: int = 50
    AI_CONTEXT_MAX_TOKENS: int = 8000
    AI_CIRCUIT_BREAKER_THRESHOLD: int = 5
    AI_CIRCUIT_BREAKER_RESET_SECONDS: float = 30.0

    # --- M4: AI orchestration -----------------------------------------------
    #: Ordered provider fallback chain used when a request does not pin a model.
    AI_FALLBACK_CHAIN: List[str] = ["openai", "local", "mock"]
    AI_FALLBACK_ENABLED: bool = True
    #: Keep the last N turns of conversation memory in a workflow run.
    AI_MEMORY_MAX_TURNS: int = 20
    #: USD per 1000 tokens, used when a model has no explicit pricing config.
    AI_DEFAULT_PROMPT_COST_PER_1K: float = 0.0
    AI_DEFAULT_COMPLETION_COST_PER_1K: float = 0.0
    #: Retain at most N AI traces in memory for the /api/ai/traces endpoint.
    AI_TRACE_BUFFER_SIZE: int = 200

    # --- Media pipeline -----------------------------------------------------
    MEDIA_ROOT: str = "./media_storage"
    MEDIA_MAX_FILE_BYTES: int = 2 * 1024 * 1024 * 1024
    MEDIA_MAX_CONCURRENT_JOBS: int = 2
    MEDIA_THUMBNAIL_SIZE: int = 320
    FFMPEG_BINARY: str = "ffmpeg"
    FFPROBE_BINARY: str = "ffprobe"
    FFMPEG_TIMEOUT_SECONDS: float = 900.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Swap in the list-friendly env sources (M6-F1).

        Order is unchanged from the pydantic-settings default, so precedence
        (init > env > .env > secrets) behaves exactly as before.
        """
        return (
            init_settings,
            _ListFriendlyEnvSource(settings_cls),
            _ListFriendlyDotEnvSource(settings_cls),
            file_secret_settings,
        )

    @field_validator(
        "CORS_ORIGINS",
        "SHELL_ALLOWED_COMMANDS",
        "HTTP_EXECUTOR_ALLOWED_HOSTS",
        "AI_FALLBACK_CHAIN",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value):
        """Allow comma-separated env values, e.g. CORS_ORIGINS=a,b,c."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return value  # let pydantic parse JSON
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}

    @property
    def is_testing(self) -> bool:
        return self.ENVIRONMENT.lower() in {"test", "testing"}

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def media_root_path(self) -> Path:
        return Path(self.MEDIA_ROOT).expanduser().resolve()


settings = Settings()
