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

    @field_validator("CORS_ORIGINS", "SHELL_ALLOWED_COMMANDS", "HTTP_EXECUTOR_ALLOWED_HOSTS", mode="before")
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
