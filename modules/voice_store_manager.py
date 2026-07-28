"""Voice store manager: catalog sync + voice model install/uninstall.

Optional BaseModule (registry priority 18, CAN BE DISABLED: YES). Has no
File 07 module spec; built from the surrounding contract pieces:
the ``voice_store_cache`` table (schema.sql: cached catalog with
download_url, ram_required_mb, is_installed), the ``installed_voices``
table (UNIQUE(engine, voice_name), model_file_path, total_uses),
``config/voice_store_catalog.json`` (bundled offline catalog; File 12
"STEP 6 Add voices to voice_store_catalog.json"), and the engines
layout used by tts_engine_manager (``engines/<engine>/models``).

Offline-first: the bundled JSON catalog works with zero network; a
remote catalog URL (or injected catalog data) can refresh the cache.
All network/IO failures degrade to graceful make_response errors
(RULE 7). RULE 1: no other module is imported; engine model paths are
derived from configs + project root only (documented duplication of the
tts_engine_manager layout constants).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from core.safe_io import LazyModule
from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

psutil = LazyModule("psutil")
requests = LazyModule("requests")

MODULE_NAME = "voice_store_manager"

_REQUEST_TIMEOUT = 30
_DOWNLOAD_CHUNK = 65536
_SIZE_TOLERANCE = 0.20  # catalog vs actual size drift warning threshold
_SUPPORTED_ENGINES = ("piper", "kokoro", "xtts")
_CATALOG_ID = "{engine}:{voice_name}"


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


class VoiceStoreManager(BaseModule):
    """Manage the voice store catalog cache and installed voice models."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize manager; resolve project root for model installs."""
        super().__init__(container, MODULE_NAME)
        self._project_root = Path.cwd()
        # Same resolution as tts_engine_manager (documented RULE 1
        # duplication): engines/ lives beside the config folder.
        try:
            cfg_folder = getattr(self.config, "config_folder", None)
            if cfg_folder is not None:
                self._project_root = Path(cfg_folder).resolve().parent
        except Exception:  # noqa: BLE001
            pass

    def is_optional_module(self) -> bool:
        """The voice store is optional (registry required: false)."""
        return True

    # ------------------------------------------------------------------
    # Catalog handling
    # ------------------------------------------------------------------
    def _validate_catalog(self, data: Any) -> List[Dict[str, Any]]:
        """Return validated voice dicts from catalog data (RULE 8)."""
        if not isinstance(data, dict):
            raise ValueError("catalog root must be a JSON object")
        voices = data.get("voices")
        if not isinstance(voices, list):
            raise ValueError("catalog 'voices' must be a list")
        valid: List[Dict[str, Any]] = []
        for entry in voices:
            if not isinstance(entry, dict):
                continue
            if not entry.get("voice_name") or not entry.get("engine"):
                continue
            if not entry.get("download_url"):
                continue
            if str(entry["engine"]) not in _SUPPORTED_ENGINES:
                continue
            valid.append(entry)
        return valid

    def _upsert_cache(self, voices: List[Dict[str, Any]]) -> Dict[str, int]:
        """Upsert validated voices into voice_store_cache."""
        added = 0
        updated = 0
        now = utc_now_str()
        for voice in voices:
            cache_id = _CATALOG_ID.format(
                engine=voice["engine"], voice_name=voice["voice_name"]
            )
            existing = self.db.db.fetch_one(
                "SELECT id FROM voice_store_cache WHERE id = ?", (cache_id,)
            )
            fields = (
                cache_id,
                str(voice["voice_name"]),
                str(voice.get("display_name") or voice["voice_name"]),
                str(voice["engine"]),
                str(voice.get("language") or "en"),
                str(voice.get("accent") or "us"),
                str(voice.get("gender") or "male"),
                str(voice.get("style") or "documentary"),
                int(voice.get("quality_rating") or 4),
                str(voice["download_url"]),
                str(voice.get("preview_url") or "") or None,
                float(voice.get("file_size_mb") or 0.0),
                int(voice.get("ram_required_mb") or 512),
                str(voice.get("supported_emotions") or ""),
                str(voice.get("description") or ""),
                str(voice.get("tags") or ""),
                1 if voice.get("is_featured") else 0,
                now,
            )
            if existing:
                self.db.db.execute(
                    "UPDATE voice_store_cache SET voice_name = ?,"
                    " display_name = ?, engine = ?, language = ?, accent = ?,"
                    " gender = ?, style = ?, quality_rating = ?,"
                    " download_url = ?, preview_url = ?, file_size_mb = ?,"
                    " ram_required_mb = ?, supported_emotions = ?,"
                    " description = ?, tags = ?, is_featured = ?,"
                    " catalog_updated_at = ? WHERE id = ?",
                    fields[1:] + (cache_id,),
                )
                updated += 1
            else:
                self.db.db.execute(
                    "INSERT INTO voice_store_cache (id, voice_name,"
                    " display_name, engine, language, accent, gender, style,"
                    " quality_rating, download_url, preview_url,"
                    " file_size_mb, ram_required_mb, supported_emotions,"
                    " description, tags, is_installed, is_featured,"
                    " catalog_updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " ?, 0, ?, ?)",
                    fields[:-2]
                    + (fields[-2], fields[-1]),
                )
                added += 1
        return {"added": added, "updated": updated}

    def refresh_catalog(
        self,
        catalog_url: Optional[str] = None,
        catalog_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Refresh voice_store_cache from a remote or injected catalog.

        Offline-first: with no URL/data given, the bundled
        config/voice_store_catalog.json is (re)loaded. Network failures
        fall back to the bundled catalog with a warning instead of
        failing hard (RULE 7).
        """
        started = time.perf_counter()
        warnings: List[str] = []
        source = "bundled"
        data: Optional[Dict[str, Any]] = catalog_data
        if data is None and catalog_url:
            try:
                response = requests.get(catalog_url, timeout=_REQUEST_TIMEOUT)
                response.raise_for_status()
                data = response.json()
                source = catalog_url
            except (requests.RequestException, ValueError) as exc:
                warnings.append(
                    f"remote catalog fetch failed ({exc}); using bundled"
                )
                data = None
        if data is None:
            data = self.config.get_config("voice_store_catalog") or {}

        try:
            voices = self._validate_catalog(data)
        except ValueError as exc:
            return self.make_response(
                False, error=f"invalid catalog: {exc}", duration_ms=_ms(started)
            )
        counts = self._upsert_cache(voices)
        total = self.db.db.fetch_one(
            "SELECT COUNT(*) AS n FROM voice_store_cache"
        )
        return self.make_response(
            True,
            {
                "source": source,
                "added": counts["added"],
                "updated": counts["updated"],
                "total_cached": int((total or {}).get("n") or 0),
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def _ensure_cache_loaded(self) -> None:
        """Lazy-load the bundled catalog when the cache table is empty."""
        row = self.db.db.fetch_one("SELECT COUNT(*) AS n FROM voice_store_cache")
        if int((row or {}).get("n") or 0) == 0:
            self.refresh_catalog()

    # ------------------------------------------------------------------
    # Browsing
    # ------------------------------------------------------------------
    def list_voices(
        self,
        engine: Optional[str] = None,
        gender: Optional[str] = None,
        language: Optional[str] = None,
        featured: Optional[bool] = None,
        installed: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """List cached catalog voices with optional filters."""
        started = time.perf_counter()
        self._ensure_cache_loaded()
        clauses: List[str] = []
        params: List[Any] = []
        if engine:
            clauses.append("engine = ?")
            params.append(engine)
        if gender:
            clauses.append("gender = ?")
            params.append(gender)
        if language:
            clauses.append("language = ?")
            params.append(language)
        if featured is not None:
            clauses.append("is_featured = ?")
            params.append(1 if featured else 0)
        if installed is not None:
            clauses.append("is_installed = ?")
            params.append(1 if installed else 0)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.db.fetch_all(
            "SELECT * FROM voice_store_cache"
            f"{where} ORDER BY is_featured DESC, quality_rating DESC, voice_name",
            tuple(params),
        )
        return self.make_response(
            True, {"count": len(rows), "voices": rows}, duration_ms=_ms(started)
        )

    def search_voices(self, query: str) -> Dict[str, Any]:
        """Search catalog voices by name/display/tags/description."""
        started = time.perf_counter()
        self._ensure_cache_loaded()
        needle = f"%{str(query or '').strip()}%"
        rows = self.db.db.fetch_all(
            "SELECT * FROM voice_store_cache WHERE voice_name LIKE ?"
            " OR display_name LIKE ? OR tags LIKE ? OR description LIKE ?"
            " ORDER BY is_featured DESC, quality_rating DESC, voice_name",
            (needle, needle, needle, needle),
        )
        return self.make_response(
            True,
            {"query": query, "count": len(rows), "voices": rows},
            duration_ms=_ms(started),
        )

    def get_voice(self, voice_id: str) -> Dict[str, Any]:
        """Get one cached catalog voice by its 'engine:voice_name' id."""
        started = time.perf_counter()
        self._ensure_cache_loaded()
        row = self.db.db.fetch_one(
            "SELECT * FROM voice_store_cache WHERE id = ?", (str(voice_id),)
        )
        if row is None:
            return self.make_response(
                False, error=f"Voice not in catalog: {voice_id}"
            )
        return self.make_response(
            True, {"voice": row}, duration_ms=_ms(started)
        )

    # ------------------------------------------------------------------
    # Install / uninstall
    # ------------------------------------------------------------------
    def _models_dir(self, engine: str, voice_name: str) -> Path:
        """engines/<engine>/models/<voice_name> (tts layout contract)."""
        return self._project_root / "engines" / engine / "models" / voice_name

    @staticmethod
    def _filename_from_url(url: str, voice_name: str) -> str:
        tail = Path(urlparse(str(url)).path).name
        return tail or f"{voice_name}.onnx"

    def install_voice(
        self,
        voice_id: str,
        force: bool = False,
        session: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Download and install a catalog voice into installed_voices.

        The service container has no HTTP service, so requests is used
        directly here; tests inject a fake ``session`` exposing
        ``get(url, stream=True, timeout=...)``. Progress is published as
        ``voice_store.download_progress`` events. RAM pressure is a
        warning, never a blocker (the file itself is small).
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(
                False, error="voice_store_manager is disabled"
            )
        voice_result = self.get_voice(voice_id)
        if not voice_result["success"]:
            return voice_result
        voice = voice_result["data"]["voice"]
        warnings: List[str] = []

        already = self.db.db.fetch_one(
            "SELECT id FROM installed_voices WHERE engine = ? AND voice_name = ?",
            (voice["engine"], voice["voice_name"]),
        )
        if already and not force:
            return self.make_response(
                False,
                error=(
                    f"voice already installed: {voice['voice_name']}"
                    " (use force=True to reinstall)"
                ),
            )
        if already:
            # Reinstall: drop the old row/files BEFORE downloading (the
            # model lands at the same path, so old-file cleanup must not
            # run after the new download).
            self._remove_installed_files(already["id"], warnings)
            self.db.db.execute(
                "DELETE FROM installed_voices WHERE id = ?", (already["id"],)
            )

        free_mb = psutil.virtual_memory().available / (1024 * 1024)
        if free_mb < float(voice.get("ram_required_mb") or 0):
            warnings.append(
                f"voice needs {voice['ram_required_mb']} MB RAM at"
                f" synth time; only {int(free_mb)} MB free"
            )

        target_dir = self._models_dir(voice["engine"], voice["voice_name"])
        filename = self._filename_from_url(
            str(voice["download_url"]), str(voice["voice_name"])
        )
        target_file = target_dir / filename
        client = session or requests
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            response = client.get(
                str(voice["download_url"]), stream=True, timeout=_REQUEST_TIMEOUT
            )
            response.raise_for_status()
            downloaded = 0
            with target_file.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    self.event_bus.publish(
                        "voice_store.download_progress",
                        {
                            "voice_id": voice_id,
                            "downloaded_mb": round(
                                downloaded / (1024 * 1024), 3
                            ),
                            "expected_mb": voice.get("file_size_mb"),
                        },
                    )
        except (requests.RequestException, OSError) as exc:
            try:
                target_file.unlink(missing_ok=True)
            except OSError:
                pass
            return self.make_response(
                False,
                error=f"download failed: {exc}",
                duration_ms=_ms(started),
            )

        size_bytes = target_file.stat().st_size
        if size_bytes == 0:
            target_file.unlink(missing_ok=True)
            return self.make_response(
                False, error="download produced an empty file",
                duration_ms=_ms(started),
            )
        actual_mb = size_bytes / (1024 * 1024)
        expected_mb = float(voice.get("file_size_mb") or 0.0)
        if expected_mb > 0:
            drift = abs(actual_mb - expected_mb) / expected_mb
            if drift > _SIZE_TOLERANCE:
                warnings.append(
                    f"size drift: catalog {expected_mb} MB vs actual"
                    f" {round(actual_mb, 2)} MB"
                )

        installed_id = self.db.new_id()
        now = utc_now_str()
        self.db.db.execute(
            "INSERT INTO installed_voices (id, voice_name,"
            " voice_display_name, engine, language, accent, gender, style,"
            " quality_rating, model_file_path, config_file_path,"
            " model_size_mb, ram_required_mb, supported_emotions, is_cloned,"
            " clone_sample_path, installed_at, last_used_at, total_uses,"
            " is_enabled, store_voice_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 0, NULL,"
            " ?, NULL, 0, 1, ?)",
            (
                installed_id,
                voice["voice_name"],
                voice["display_name"],
                voice["engine"],
                voice["language"],
                voice["accent"],
                voice["gender"],
                voice["style"],
                min(5, max(1, int(voice.get("quality_rating") or 4))),
                str(target_file),
                round(actual_mb, 3),
                int(voice.get("ram_required_mb") or 512),
                str(voice.get("supported_emotions") or ""),
                now,
                str(voice_id),
            ),
        )
        self.db.db.execute(
            "UPDATE voice_store_cache SET is_installed = 1 WHERE id = ?",
            (str(voice_id),),
        )
        self.event_bus.publish(
            "voice_store.voice_installed",
            {"voice_id": voice_id, "installed_id": installed_id},
        )
        self.log.info(
            "Installed voice %s for engine %s -> %s",
            voice["voice_name"],
            voice["engine"],
            target_file,
        )
        return self.make_response(
            True,
            {
                "installed_id": installed_id,
                "voice_id": voice_id,
                "engine": voice["engine"],
                "voice_name": voice["voice_name"],
                "model_file_path": str(target_file),
                "model_size_mb": round(actual_mb, 3),
                "reinstalled": bool(already),
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def _remove_installed_files(
        self, installed_id: str, warnings: List[str]
    ) -> None:
        """Delete the model file recorded for an installed-voices row."""
        row = self.db.db.fetch_one(
            "SELECT model_file_path FROM installed_voices WHERE id = ?",
            (installed_id,),
        )
        if not row:
            return
        target = Path(str(row.get("model_file_path") or ""))
        try:
            target.unlink(missing_ok=True)
            # Remove the voice dir when we emptied it (never parents).
            if target.parent.is_dir() and not any(target.parent.iterdir()):
                target.parent.rmdir()
        except OSError as exc:
            warnings.append(f"file removal failed: {exc}")

    def uninstall_voice(self, voice_id_or_installed_id: str) -> Dict[str, Any]:
        """Uninstall a voice by catalog id or installed_voices row id."""
        started = time.perf_counter()
        row = self.db.db.fetch_one(
            "SELECT * FROM installed_voices WHERE id = ? OR store_voice_id = ?",
            (str(voice_id_or_installed_id), str(voice_id_or_installed_id)),
        )
        if row is None:
            return self.make_response(
                False, error=f"Voice not installed: {voice_id_or_installed_id}"
            )
        warnings: List[str] = []
        self._remove_installed_files(row["id"], warnings)
        self.db.db.execute(
            "DELETE FROM installed_voices WHERE id = ?", (row["id"],)
        )
        if row.get("store_voice_id"):
            self.db.db.execute(
                "UPDATE voice_store_cache SET is_installed = 0 WHERE id = ?",
                (row["store_voice_id"],),
            )
        self.event_bus.publish(
            "voice_store.voice_uninstalled",
            {"voice_id": row.get("store_voice_id"), "engine": row["engine"]},
        )
        return self.make_response(
            True,
            {
                "uninstalled_id": row["id"],
                "engine": row["engine"],
                "voice_name": row["voice_name"],
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Installed inventory
    # ------------------------------------------------------------------
    def list_installed(self, engine: Optional[str] = None) -> Dict[str, Any]:
        """List installed voices (optionally one engine)."""
        started = time.perf_counter()
        if engine:
            rows = self.db.db.fetch_all(
                "SELECT * FROM installed_voices WHERE engine = ?"
                " ORDER BY voice_name",
                (engine,),
            )
        else:
            rows = self.db.db.fetch_all(
                "SELECT * FROM installed_voices ORDER BY engine, voice_name"
            )
        return self.make_response(
            True, {"count": len(rows), "voices": rows}, duration_ms=_ms(started)
        )

    def get_installed_voice(
        self, engine: str, voice_name: str
    ) -> Dict[str, Any]:
        """Get one installed voice by (engine, voice_name)."""
        started = time.perf_counter()
        row = self.db.db.fetch_one(
            "SELECT * FROM installed_voices WHERE engine = ? AND voice_name = ?",
            (engine, voice_name),
        )
        if row is None:
            return self.make_response(
                False, error=f"Voice not installed: {engine}/{voice_name}"
            )
        return self.make_response(
            True, {"voice": row}, duration_ms=_ms(started)
        )

    def record_voice_usage(self, engine: str, voice_name: str) -> Dict[str, Any]:
        """Increment total_uses and stamp last_used_at for a voice.

        Called by the app orchestrator after a successful synthesis
        (RULE 1 keeps this module free of tts_engine_manager imports).
        """
        started = time.perf_counter()
        row = self.db.db.fetch_one(
            "SELECT id, total_uses FROM installed_voices"
            " WHERE engine = ? AND voice_name = ?",
            (engine, voice_name),
        )
        if row is None:
            return self.make_response(
                False, error=f"Voice not installed: {engine}/{voice_name}"
            )
        self.db.db.execute(
            "UPDATE installed_voices SET total_uses = ?, last_used_at = ?"
            " WHERE id = ?",
            (int(row.get("total_uses") or 0) + 1, utc_now_str(), row["id"]),
        )
        return self.make_response(
            True,
            {
                "id": row["id"],
                "total_uses": int(row.get("total_uses") or 0) + 1,
            },
            duration_ms=_ms(started),
        )

    def set_voice_enabled(
        self, installed_id: str, enabled: bool
    ) -> Dict[str, Any]:
        """Enable/disable an installed voice without uninstalling."""
        started = time.perf_counter()
        row = self.db.db.fetch_one(
            "SELECT id FROM installed_voices WHERE id = ?", (str(installed_id),)
        )
        if row is None:
            return self.make_response(
                False, error=f"Installed voice not found: {installed_id}"
            )
        self.db.db.execute(
            "UPDATE installed_voices SET is_enabled = ? WHERE id = ?",
            (1 if enabled else 0, str(installed_id)),
        )
        return self.make_response(
            True,
            {"id": str(installed_id), "is_enabled": bool(enabled)},
            duration_ms=_ms(started),
        )

    def get_store_stats(self) -> Dict[str, Any]:
        """Catalog/install counts split by engine."""
        started = time.perf_counter()
        self._ensure_cache_loaded()
        catalog = self.db.db.fetch_one(
            "SELECT COUNT(*) AS n FROM voice_store_cache"
        )
        installed = self.db.db.fetch_all(
            "SELECT engine, COUNT(*) AS n FROM installed_voices"
            " GROUP BY engine"
        )
        featured = self.db.db.fetch_one(
            "SELECT COUNT(*) AS n FROM voice_store_cache WHERE is_featured = 1"
        )
        return self.make_response(
            True,
            {
                "catalog_size": int((catalog or {}).get("n") or 0),
                "installed_count": sum(int(r["n"]) for r in installed),
                "installed_by_engine": {
                    r["engine"]: int(r["n"]) for r in installed
                },
                "featured_count": int((featured or {}).get("n") or 0),
            },
            duration_ms=_ms(started),
        )

