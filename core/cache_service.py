"""Disk-backed caching layer for Autopilot.

Caches expensive intermediate results (proxy images, analysis, etc.)
with TTL and max-size eviction. Prefer disk over RAM per lazy-loading rules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.safe_io import (
    atomic_write,
    atomic_write_json,
    purge_stale_temp_files,
    quarantine_corrupt_file,
    safe_unlink,
    writable_directory,
)

logger = logging.getLogger("autopilot.cache")


class CacheService:
    """File-system cache with metadata index and size limits."""

    def __init__(
        self,
        cache_folder: str | Path = "cache",
        max_size_mb: int = 2048,
        default_ttl_seconds: int = 7 * 24 * 3600,
    ) -> None:
        """Initialize cache service.

        Args:
            cache_folder: Root cache directory.
            max_size_mb: Maximum total cache size in megabytes.
            default_ttl_seconds: Default entry lifetime.
        """
        # PHASE 9: the cache is an OPTIONAL accelerator — an unusable
        # folder (read-only install dir, removed network drive) must
        # degrade to a temp location rather than crash construction of
        # the service container at boot.
        self.cache_folder = writable_directory(cache_folder, "autopilot-cache")
        self.max_size_bytes = max(0, int(max_size_mb)) * 1024 * 1024
        self.default_ttl_seconds = default_ttl_seconds
        self._lock = threading.RLock()
        self._index_path = self.cache_folder / "cache_index.json"
        # PHASE 9: reclaim temp files left behind by a killed render.
        purge_stale_temp_files(self.cache_folder)
        self._index: Dict[str, Dict[str, Any]] = self._load_index()
        # PHASE 8 (rendering & export optimization): a plain read used to
        # rewrite the whole index file just to record last_access. On a
        # cache with thousands of entries that is a full JSON serialize
        # plus a disk write per cache HIT. last_access only orders LRU
        # eviction, so it is updated in memory and flushed with the next
        # structural change (set/delete/clear) or once the interval below
        # has elapsed — the on-disk index still converges, and a lost
        # update can at worst evict in a slightly different order.
        self._index_flushed_at = time.time()

    def _load_index(self) -> Dict[str, Dict[str, Any]]:
        """Load cache index from disk, discarding unusable entries.

        PHASE 9 (corrupt cache recovery): a truncated index used to make
        every subsequent read return None forever, because the corrupt
        file was left in place and re-read on every launch. It is now
        quarantined once so the next save starts clean. Individual
        malformed entries are dropped rather than poisoning later reads
        (``float(meta["expires_at"])`` on a garbage value raised inside
        ``get`` and propagated to the caller).
        """
        if not self._index_path.exists():
            return {}
        try:
            with self._index_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Cache index corrupt, starting empty: %s", exc)
            quarantine_corrupt_file(self._index_path)
            return {}
        if not isinstance(data, dict):
            logger.warning("Cache index has unexpected shape, starting empty")
            quarantine_corrupt_file(self._index_path)
            return {}
        clean: Dict[str, Dict[str, Any]] = {}
        for key, meta in data.items():
            if isinstance(meta, dict) and meta.get("path"):
                clean[str(key)] = meta
        if len(clean) != len(data):
            logger.warning(
                "Dropped %d malformed cache index entries", len(data) - len(clean)
            )
        return clean

    def _touch_index(self) -> None:
        """Record a non-structural index change; flush if it's time.

        PHASE 8 (rendering & export optimization): see __init__.
        """
        if time.time() - self._index_flushed_at >= self._INDEX_FLUSH_SECONDS:
            self._save_index()

    def _save_index(self) -> None:
        """Persist cache index to disk atomically.

        PHASE 9: the index was written in place, so an interruption
        mid-write truncated it and lost the whole cache on next launch.
        It is now written to a temp file and atomically renamed.
        """
        self._index_flushed_at = time.time()
        atomic_write_json(self._index_path, self._index)

    # PHASE 8: how long a purely last_access index change may stay
    # in memory before it is written out (see _touch_index).
    _INDEX_FLUSH_SECONDS = 30.0

    # Windows-forbidden filename characters: < > : " / \ | ? * and control chars.
    _UNSAFE_FILENAME_CHARS = '<>:"/\\|?*'

    @staticmethod
    def make_key(*parts: str) -> str:
        """Build a stable cache key from string parts.

        Args:
            *parts: Key components.

        Returns:
            SHA256 hex digest (safe for filenames).
        """
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def sanitize_filename(cls, key: str, max_length: int = 180) -> str:
        """Sanitize a cache key for use as a filesystem path segment.

        Logical keys may contain characters illegal on Windows (e.g. ':').
        The original key remains the index dictionary key; only the on-disk
        name is sanitized.

        Args:
            key: Logical cache key.
            max_length: Maximum filename length before hashing.

        Returns:
            Filesystem-safe filename stem (no directory separators).
        """
        text = str(key or "empty")
        # Normalize path separators first
        text = text.replace("\\", "_").replace("/", "_")
        sanitized_chars: list[str] = []
        for char in text:
            if char in cls._UNSAFE_FILENAME_CHARS or ord(char) < 32:
                sanitized_chars.append("_")
            else:
                sanitized_chars.append(char)
        sanitized = "".join(sanitized_chars).strip(" .")
        if not sanitized:
            sanitized = "cache_entry"
        # Avoid Windows reserved device names
        reserved = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            "COM1",
            "COM2",
            "COM3",
            "COM4",
            "COM5",
            "COM6",
            "COM7",
            "COM8",
            "COM9",
            "LPT1",
            "LPT2",
            "LPT3",
            "LPT4",
            "LPT5",
            "LPT6",
            "LPT7",
            "LPT8",
            "LPT9",
        }
        if sanitized.upper() in reserved:
            sanitized = f"_{sanitized}"
        if len(sanitized) > max_length:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            sanitized = f"{sanitized[: max_length - 17]}_{digest}"
        return sanitized

    def _entry_path(self, key: str, suffix: str = ".bin") -> Path:
        """Return filesystem path for a cache key (Windows-safe filename)."""
        safe_name = self.sanitize_filename(key)
        # Shard by first two chars of the *safe* name for even distribution
        sub = safe_name[:2] if len(safe_name) >= 2 else "00"
        folder = self.cache_folder / sub
        # PHASE 9: creating the shard folder can fail (permissions, disk
        # full). The path is still returned so callers take their normal
        # write-failure branch instead of this raising out of a `get`.
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Cache shard folder unavailable %s: %s", folder, exc)
        if suffix and not suffix.startswith("."):
            suffix = f".{suffix}"
        return folder / f"{safe_name}{suffix}"

    def get(self, key: str) -> Optional[bytes]:
        """Read a binary cache entry if present and not expired.

        Args:
            key: Cache key.

        Returns:
            Bytes or None.
        """
        with self._lock:
            meta = self._index.get(key)
            if not meta:
                return None
            # PHASE 9: a hand-edited or partially-written index entry
            # could hold a non-numeric expiry; a cache read must degrade
            # to a MISS, never raise into the caller's render stage.
            try:
                expires_at = float(meta.get("expires_at", 0) or 0)
            except (TypeError, ValueError):
                logger.warning("Cache entry %s has invalid expiry — evicting", key)
                self.delete(key)
                return None
            if expires_at and time.time() > expires_at:
                self.delete(key)
                return None
            path = Path(meta.get("path", self._entry_path(key)))
            if not path.exists():
                self._index.pop(key, None)
                self._save_index()
                return None
            try:
                data = path.read_bytes()
                meta["last_access"] = time.time()
                self._touch_index()
                return data
            except OSError as exc:
                logger.error("Cache read failed for %s: %s", key, exc)
                return None

    def get_json(self, key: str) -> Optional[Any]:
        """Read a JSON cache entry.

        Args:
            key: Cache key.

        Returns:
            Parsed JSON or None.
        """
        raw = self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def set(
        self,
        key: str,
        data: bytes,
        ttl_seconds: Optional[int] = None,
        suffix: str = ".bin",
    ) -> bool:
        """Write a binary cache entry.

        Args:
            key: Cache key.
            data: Payload bytes.
            ttl_seconds: Optional TTL override.
            suffix: File suffix.

        Returns:
            True on success.
        """
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        path = self._entry_path(key, suffix=suffix)
        with self._lock:
            # PHASE 9: the payload is written to a temp file and renamed,
            # so an interruption can never leave a SHORT file that the
            # index still describes as complete — the exact way a cache
            # silently starts serving truncated data.
            if not atomic_write(path, lambda temp: temp.write_bytes(data)):
                return False
            try:
                self._index[key] = {
                    "path": str(path),
                    "size": len(data),
                    "created_at": time.time(),
                    "last_access": time.time(),
                    "expires_at": time.time() + ttl if ttl > 0 else 0,
                }
                self._save_index()
                self._enforce_size_limit()
                return True
            except OSError as exc:
                logger.error("Cache write failed for %s: %s", key, exc)
                return False

    def set_json(
        self,
        key: str,
        value: Any,
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        """Write a JSON-serializable value to cache.

        Args:
            key: Cache key.
            value: JSON-serializable object.
            ttl_seconds: Optional TTL.

        Returns:
            True on success.
        """
        # PHASE 9: a value the caller believed serializable (a set, a
        # Path, a NumPy scalar) used to raise TypeError out of a cache
        # WRITE and abort the calling stage. Caching is optional — a
        # non-serializable value is a miss, not a render failure.
        try:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            logger.warning("Cache value for %s is not JSON-serializable: %s", key, exc)
            return False
        return self.set(key, payload, ttl_seconds=ttl_seconds, suffix=".json")

    def delete(self, key: str) -> bool:
        """Delete a cache entry.

        Args:
            key: Cache key.

        Returns:
            True if removed or already absent.
        """
        with self._lock:
            meta = self._index.pop(key, None)
            self._save_index()
            if not meta:
                return True
            raw_path = str(meta.get("path") or "")
            if not raw_path:
                return True
            # PHASE 9: safe_unlink tolerates a file another process
            # already removed and never raises out of eviction.
            return safe_unlink(Path(raw_path))

    def clear(self) -> None:
        """Remove all cache entries and index."""
        with self._lock:
            for key in list(self._index.keys()):
                self.delete(key)
            self._index.clear()
            self._save_index()

    def get_size_bytes(self) -> int:
        """Return total size of tracked cache entries in bytes."""
        with self._lock:
            # PHASE 9: one malformed size (a string, None) used to raise
            # from here — and this runs inside every set()/put_file(),
            # so it could abort a write path over pure bookkeeping.
            total = 0
            for meta in self._index.values():
                try:
                    total += int(meta.get("size", 0) or 0)
                except (TypeError, ValueError):
                    continue
            return total

    def get_size_mb(self) -> float:
        """Return total cache size in megabytes."""
        return self.get_size_bytes() / 1024 / 1024

    @staticmethod
    def _as_epoch(value: Any) -> float:
        """Parse an index timestamp, treating anything unusable as oldest."""
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _enforce_size_limit(self) -> None:
        """Evict least-recently-accessed entries until under max size."""
        total = self.get_size_bytes()
        if total <= self.max_size_bytes:
            return
        # PHASE 9: a malformed last_access no longer raises out of the
        # sort key — such an entry simply sorts oldest and is evicted
        # first, which is the correct outcome for an unusable record.
        ordered = sorted(
            self._index.items(),
            key=lambda item: self._as_epoch(item[1].get("last_access")),
        )
        for key, meta in ordered:
            if total <= self.max_size_bytes:
                break
            try:
                size = int(meta.get("size", 0) or 0)
            except (TypeError, ValueError):
                size = 0
            self.delete(key)
            total -= size
            logger.debug("Evicted cache key %s (%s bytes)", key, size)

    def put_file(
        self, key: str, source_path: str | Path, ttl_seconds: Optional[int] = None
    ) -> bool:
        """Copy an existing file into the cache.

        Args:
            key: Cache key.
            source_path: Source file path.
            ttl_seconds: Optional TTL.

        Returns:
            True on success.
        """
        source = Path(source_path)
        if not source.exists():
            return False
        # PHASE 8 (rendering & export optimization): copy the file
        # through the filesystem instead of loading every byte into a
        # Python bytes object first — a cached render segment can be
        # hundreds of megabytes. Same destination bytes, same index
        # bookkeeping as the previous read-then-set path.
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        path = self._entry_path(key, suffix=source.suffix or ".bin")
        with self._lock:
            # PHASE 9: copy to a temp file and rename, so a copy
            # interrupted part-way (disk full, cancelled render) can
            # never register a half-written segment as a valid entry.
            if not atomic_write(path, lambda temp: shutil.copyfile(source, temp)):
                return False
            try:
                size = path.stat().st_size
                now = time.time()
                self._index[key] = {
                    "path": str(path),
                    "size": size,
                    "created_at": now,
                    "last_access": now,
                    "expires_at": now + ttl if ttl > 0 else 0,
                }
                self._save_index()
                self._enforce_size_limit()
                return True
            except OSError as exc:
                logger.error("Cache file copy failed for %s: %s", key, exc)
                return False

    def copy_to(self, key: str, dest_path: str | Path) -> bool:
        """Copy a cache entry to a destination path.

        Args:
            key: Cache key.
            dest_path: Destination file path.

        Returns:
            True on success.
        """
        with self._lock:
            meta = self._index.get(key)
            if not meta:
                return False
            src = Path(str(meta.get("path") or ""))
            if not src.is_file():
                return False
            dest = Path(dest_path)
            # PHASE 9: the destination is produced atomically, so a
            # consumer never observes a partially-restored cache entry;
            # an uncreatable destination folder is a clean False.
            return atomic_write(dest, lambda temp: shutil.copyfile(src, temp))
