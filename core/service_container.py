"""Dependency injection container and BaseModule for Autopilot.

All shared services are created once and injected into modules.
Modules never construct their own database or config instances.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.time_helper import utc_now_str

logger = logging.getLogger("autopilot.container")


class ServiceContainer:
    """Central registry for all shared services."""

    def __init__(self) -> None:
        """Create an empty container."""
        self._services: Dict[str, Any] = {}
        self._initialized = False

    def register(self, name: str, service: Any) -> None:
        """Register a service instance.

        Args:
            name: Service key (e.g. 'database').
            service: Service instance.
        """
        if name in self._services:
            logger.warning("Service '%s' already registered, replacing", name)
        self._services[name] = service
        logger.debug("Service registered: %s", name)

    def get(self, name: str) -> Any:
        """Get a registered service by name.

        Args:
            name: Service key.

        Returns:
            Service instance.

        Raises:
            KeyError: If service is not registered.
        """
        if name not in self._services:
            raise KeyError(f"Service '{name}' not registered in container")
        return self._services[name]

    def has(self, name: str) -> bool:
        """Check if a service is registered."""
        return name in self._services

    def get_all_service_names(self) -> List[str]:
        """Return list of all registered service names."""
        return list(self._services.keys())

    @property
    def initialized(self) -> bool:
        """Whether production initialization completed."""
        return self._initialized

    @staticmethod
    def _resolve_path(root: Path, path_value: str) -> Path:
        """Resolve a possibly-relative path against project root."""
        path = Path(path_value)
        return path if path.is_absolute() else root / path

    def _register_data_services(self, config: Dict[str, Any], root: Path) -> None:
        """Register log, database, and config services."""
        from core.config_service import ConfigService
        from core.database_service import DatabaseService, SQLiteDatabase
        from core.log_service import LogService

        self.register(
            "log",
            LogService(
                log_folder=self._resolve_path(root, config.get("log_folder", "logs"))
            ),
        )
        db_impl = SQLiteDatabase(
            db_path=self._resolve_path(
                root, config.get("database_path", "database/autopilot.db")
            ),
            schema_path=self._resolve_path(
                root, config.get("schema_path", "database/schema.sql")
            ),
        )
        db_impl.initialize()
        self.register("database", DatabaseService(db_impl))
        self.register(
            "config",
            ConfigService(
                config_folder=self._resolve_path(
                    root, config.get("config_folder", "config")
                )
            ),
        )

    def _register_runtime_services(self, config: Dict[str, Any], root: Path) -> None:
        """Register cache, hardware, and event bus services."""
        from core.cache_service import CacheService
        from core.event_bus import EventBus
        from core.hardware_service import HardwareService

        self.register(
            "cache",
            CacheService(
                cache_folder=self._resolve_path(
                    root, config.get("cache_folder", "cache")
                ),
                max_size_mb=int(config.get("cache_size_mb", 2048)),
            ),
        )
        self.register(
            "hardware",
            HardwareService(
                ffmpeg_path=config.get("ffmpeg_path", "engines/ffmpeg/ffmpeg")
            ),
        )
        self.register("event_bus", EventBus())

    def _register_core_services(self, config: Dict[str, Any], root: Path) -> None:
        """Register all production core services."""
        self._register_data_services(config, root)
        self._register_runtime_services(config, root)
        logger.info("All core services registered")

    @classmethod
    def create_production_container(
        cls,
        app_config: Optional[Dict[str, Any]] = None,
        project_root: Optional[Path] = None,
    ) -> "ServiceContainer":
        """Create container with all production services.

        Args:
            app_config: Optional path/config overrides.
            project_root: Project root for resolving relative paths.

        Returns:
            Fully registered ServiceContainer.
        """
        config = dict(app_config or {})
        root = Path(project_root) if project_root else Path.cwd()
        container = cls()
        container._register_core_services(config, root)
        container._initialized = True
        return container

    @classmethod
    def create_test_container(cls) -> "ServiceContainer":
        """Create container with mock services for unit tests.

        Returns:
            Container with MagicMock services.
        """
        from unittest.mock import MagicMock

        container = cls()
        mock_db = MagicMock()
        mock_db.fetch_one.return_value = None
        mock_db.fetch_all.return_value = []
        mock_db.execute.return_value = None
        mock_db.initialize.return_value = True
        mock_db.integrity_check.return_value = True
        mock_db.verify_product_tables.return_value = (True, [])
        container.register("database", mock_db)
        container.register("config", MagicMock())
        container.register("cache", MagicMock())
        container.register("hardware", MagicMock())
        container.register("log", MagicMock())
        container.register("event_bus", MagicMock())
        container._initialized = True
        return container


class BaseModule:
    """Base class for all Autopilot modules.

    Provides standard access to injected services and a response helper.
    """

    def __init__(self, container: ServiceContainer, module_name: str) -> None:
        """Initialize module with DI container.

        Args:
            container: Service container.
            module_name: Stable module name for logs and responses.
        """
        self.container = container
        self.module_name = module_name
        self.db = container.get("database")
        self.config = container.get("config")
        self.cache = container.get("cache")
        self.hardware = container.get("hardware")
        self.event_bus = container.get("event_bus")
        self.log = logging.getLogger(f"autopilot.{module_name}")
        self._enabled = True

    @property
    def enabled(self) -> bool:
        """Whether this module is enabled."""
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the module at runtime."""
        self._enabled = enabled

    def make_response(
        self,
        success: bool,
        data: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        warnings: Optional[List[str]] = None,
        duration_ms: float = 0.0,
    ) -> Dict[str, Any]:
        """Build the standard module response object.

        Args:
            success: Whether the operation succeeded.
            data: Result payload.
            error: Error message if failed.
            warnings: Optional warnings list.
            duration_ms: Operation duration in milliseconds.

        Returns:
            Standard response dictionary.
        """
        return {
            "success": success,
            "data": data if data is not None else {},
            "error": error,
            "warnings": warnings if warnings is not None else [],
            "module": self.module_name,
            "timestamp": utc_now_str(),
            "duration_ms": duration_ms,
        }
