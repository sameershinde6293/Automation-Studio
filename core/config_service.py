"""Unified configuration service for Autopilot.

Loads JSON config files from the config folder once and provides typed
access. Missing or corrupt files fall back to in-memory defaults.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.safe_io import atomic_write_json, ensure_directory

logger = logging.getLogger("autopilot.config")

CONFIG_FILES: Tuple[str, ...] = (
    "app_settings.json",
    "modules_config.json",
    "keyboard_shortcuts.json",
    "export_presets.json",
    "color_grade_presets.json",
    "transition_presets.json",
    "animation_presets.json",
    "subtitle_style_presets.json",
    "sfx_config.json",
    "voice_store_catalog.json",
    "documentary_genres.json",
    "keyword_emotion_map.json",
    "ffmpeg_commands.json",
    "default_channel_profile.json",
    "drive_upload.json",
    "plugins_config.json",
)


class ConfigService:
    """Loads and caches all JSON configuration files."""

    def __init__(self, config_folder: str | Path = "config") -> None:
        """Initialize config service.

        Args:
            config_folder: Path to the config directory.
        """
        self.config_folder = Path(config_folder)
        self._cache: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._load_all()

    def _load_all(self) -> None:
        """Load every known config file into the cache."""
        # PHASE 9: a read-only or missing config folder must not stop the
        # app from booting — each file then simply falls back to its
        # in-memory default below, exactly as a missing file already did.
        ensure_directory(self.config_folder)
        for filename in CONFIG_FILES:
            self._load_file(filename)

    def _load_file(self, filename: str) -> Any:
        """Load one JSON file into cache with safe fallbacks.

        Args:
            filename: Config file name (e.g. app_settings.json).

        Returns:
            Parsed JSON object or empty dict/list default.
        """
        path = self.config_folder / filename
        key = filename.replace(".json", "")
        try:
            if not path.exists():
                logger.warning("Config missing: %s — using empty default", path)
                default = self._default_for(filename)
                self._cache[key] = default
                return default
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            # PHASE 9: a syntactically valid file can still hold the
            # wrong SHAPE (a JSON list where a settings object is
            # expected, or a bare string). Every consumer here does
            # ``isinstance(cfg, dict)``-style checks and would silently
            # fall back; substituting the documented default instead
            # keeps the failure visible in the log and the cache typed.
            if not isinstance(data, (dict, list)):
                logger.error(
                    "Config %s has unexpected type %s — using default",
                    filename,
                    type(data).__name__,
                )
                data = self._default_for(filename)
            self._cache[key] = data
            logger.debug("Loaded config: %s", filename)
            return data
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # PHASE 9: a corrupt config is recoverable — fall back to the
            # documented default rather than an empty dict, so a truncated
            # presets file still presents the shape its consumers expect.
            logger.error("Config file corrupted %s: %s — using default", path, exc)
            default = self._default_for(filename)
            self._cache[key] = default
            return default
        except OSError as exc:
            logger.error("Config file unreadable %s: %s — using default", path, exc)
            default = self._default_for(filename)
            self._cache[key] = default
            return default

    @staticmethod
    def _default_for(filename: str) -> Any:
        """In-memory fallback for a missing or unusable config file.

        PHASE 9: previously inlined in ``_load_file``'s missing-file
        branch only; corrupt and wrong-typed files fell back to a bare
        ``{}``. One helper now serves all three paths so the fallback
        shape is identical however the file failed.
        """
        if filename in (
            "export_presets.json",
            "color_grade_presets.json",
            "transition_presets.json",
            "animation_presets.json",
            "subtitle_style_presets.json",
        ):
            return {"presets": []}
        if filename == "documentary_genres.json":
            return {"genres": []}
        if "presets" in filename or filename.endswith("_map.json"):
            return []
        return {}

    def reload(self, filename: Optional[str] = None) -> None:
        """Reload one file or all config files.

        Args:
            filename: Optional specific file; None reloads all.
        """
        with self._lock:
            if filename is None:
                self._cache.clear()
                self._load_all()
            else:
                self._load_file(filename)

    def get_config(self, name: str) -> Any:
        """Return a full config object by short name.

        Args:
            name: Name without .json (e.g. 'app_settings').

        Returns:
            Cached config data.
        """
        with self._lock:
            if name not in self._cache:
                self._load_file(f"{name}.json")
            return self._cache.get(name, {})

    def get(self, key: str, default: Any = None) -> Any:
        """Get a top-level app_settings value.

        Args:
            key: Setting key.
            default: Fallback value.

        Returns:
            Setting value or default.
        """
        settings = self.get_config("app_settings")
        if isinstance(settings, dict):
            # Support nested under "settings" or flat
            if key in settings:
                return settings[key]
            nested = settings.get("settings", {})
            if isinstance(nested, dict) and key in nested:
                return nested[key]
        return default

    def set(self, key: str, value: Any, persist: bool = True) -> bool:
        """Set an app_settings value and optionally write to disk.

        Args:
            key: Setting key.
            value: New value.
            persist: Write app_settings.json if True.

        Returns:
            True on success.
        """
        with self._lock:
            settings = self._cache.get("app_settings")
            if not isinstance(settings, dict):
                settings = {}
                self._cache["app_settings"] = settings
            settings[key] = value
            if not persist:
                return True
            return self._write_json("app_settings.json", settings)

    def get_module_config(self, module_name: str) -> Optional[Dict[str, Any]]:
        """Return module entry from modules_config.json.

        Args:
            module_name: Module name (e.g. 'file_parser').

        Returns:
            Module dict or None.
        """
        modules_cfg = self.get_config("modules_config")
        modules = (
            modules_cfg.get("modules", []) if isinstance(modules_cfg, dict) else []
        )
        for entry in modules:
            if isinstance(entry, dict) and entry.get("name") == module_name:
                return entry
        return None

    def is_module_enabled(self, module_name: str) -> bool:
        """Return whether a module is enabled.

        Args:
            module_name: Module name.

        Returns:
            True if enabled or not listed (default allow).
        """
        entry = self.get_module_config(module_name)
        if entry is None:
            return True
        return bool(entry.get("enabled", True))

    def list_config_files(self) -> List[str]:
        """Return names of config files present on disk.

        Returns:
            List of filenames.
        """
        return [path.name for path in sorted(self.config_folder.glob("*.json"))]

    def validate_all_json(self) -> Dict[str, bool]:
        """Validate each expected config file as parseable JSON.

        Returns:
            Map of filename -> valid bool.
        """
        results: Dict[str, bool] = {}
        for filename in CONFIG_FILES:
            path = self.config_folder / filename
            if not path.exists():
                results[filename] = False
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    json.load(handle)
                results[filename] = True
            except (OSError, json.JSONDecodeError):
                results[filename] = False
        return results

    def _write_json(self, filename: str, data: Any) -> bool:
        """Write a config file atomically (crash-safe).

        PHASE 9: the previous implementation wrote to a FIXED
        ``<name>.tmp`` sibling, so two threads persisting settings at
        once could interleave into the same temp file and publish a
        mixture of both. It also never flushed to the device before
        renaming, and a serialization error truncated the temp file
        after the destination had already been targeted.
        ``core.safe_io.atomic_write_json`` serializes first, writes to a
        unique temp file, fsyncs, then atomically replaces — so a reader
        always sees a complete file and a failed write leaves the
        previous settings intact.

        Args:
            filename: Target file name.
            data: JSON-serializable data.

        Returns:
            True on success.
        """
        return atomic_write_json(
            self.config_folder / filename, data, trailing_newline=True
        )
