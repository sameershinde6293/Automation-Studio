"""Application settings.

All values are overridable via environment variables or a local ``.env`` file.
Backwards compatible with V1.0: ``APP_NAME``, ``VERSION``, ``ENVIRONMENT`` and
``DATABASE_URL`` keep their original names and defaults.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.version import __version__


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
    #: How long a PAUSED execution may sit before being auto-cancelled.
    EXECUTION_MAX_PAUSE_SECONDS: float = 86400.0
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

    # --- M4: script node executors (NOT sandboxes - see EXECUTION_ENGINE.md) -
    #: Python node uses a restricted-builtins interpreter. Disabled by default.
    ALLOW_PYTHON_EXECUTOR: bool = False
    PYTHON_EXECUTOR_TIMEOUT_SECONDS: float = 30.0
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
