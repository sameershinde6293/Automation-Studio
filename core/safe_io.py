"""Crash-safe filesystem primitives shared by every Autopilot layer.

PHASE 9 (defensive programming & reliability). Autopilot writes files
that a later run TRUSTS: config JSON, the cache index, processed image
frames, narration WAVs, SRT sidecars. Every one of those was written
straight over the destination path, so an interruption at the wrong
moment (power loss, a killed render, a full disk, an antivirus lock)
left a TRUNCATED file that still ``exists()`` — and the resume paths
then happily treated it as finished work.

Everything here is stdlib-only and lives in ``core/`` (like
``time_helper``/``errors``/``narration_pacing``) so modules can share it
without importing each other (RULE 1). The contract is uniform:

* writes go to a unique sibling temp file, are flushed + fsynced, then
  atomically ``os.replace``d onto the destination — a reader therefore
  sees either the complete old file or the complete new one, never a
  half-written one;
* the temp file is removed on every failure path, so a failed write
  never litters the project folder;
* Windows ``os.replace`` can transiently fail with a sharing violation
  while an indexer/antivirus holds the destination open — that specific
  case is retried briefly instead of failing the render;
* nothing here raises for cleanup-style operations (``safe_unlink``,
  ``purge_stale_temp_files``); they are best-effort by design.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("autopilot.safe_io")

# Marker shared by every temp file this module creates, so a crashed
# run's leftovers are identifiable (and purgeable) later.
TEMP_MARKER = ".ap-tmp"

# Windows can hold a brief lock on a destination file (search indexer,
# antivirus, a preview handler). Retrying a few times over ~1s clears
# the overwhelming majority of those without masking a real failure.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.2


def _temp_path_for(path: Path) -> Path:
    """Unique sibling temp path that preserves ``path``'s extension.

    The suffix is preserved because libraries that infer a format from
    the file name (soundfile, Pillow) must produce exactly the bytes
    they would have produced writing the destination directly.
    """
    unique = f"{os.getpid()}-{time.monotonic_ns():x}"
    return path.with_name(f"{path.name}{TEMP_MARKER}{unique}{path.suffix}")


def safe_unlink(path: str | Path | None) -> bool:
    """Delete a file if present; never raise.

    Args:
        path: File to remove (None is accepted and ignored).

    Returns:
        True when the path is absent afterwards.
    """
    if path is None:
        return True
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.debug("Could not remove %s: %s", path, exc)
        return False


def replace_atomic(source: str | Path, destination: str | Path) -> None:
    """``os.replace`` with a short retry for transient Windows locks.

    Args:
        source: Existing file to move into place.
        destination: Final path (overwritten atomically).

    Raises:
        OSError: If every attempt fails.
    """
    src, dst = str(source), str(destination)
    last: Optional[OSError] = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:  # Windows sharing violation
            last = exc
            time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))
        except OSError:
            raise
    raise last if last is not None else OSError(f"could not replace {dst}")


def atomic_write(
    path: str | Path,
    writer: Callable[[Path], Any],
    *,
    fsync: bool = True,
) -> bool:
    """Produce ``path`` atomically via a caller-supplied writer.

    ``writer`` is handed the temp path and writes the complete file to
    it (any library may be used — the extension is preserved). Only when
    it returns without raising is the temp file moved onto ``path``.

    Args:
        path: Final destination path.
        writer: Callable that fully writes the file it is given.
        fsync: Flush the temp file to the physical device first.

    Returns:
        True on success, False when the write or the replace failed
        (the destination is then left exactly as it was).
    """
    final = Path(path)
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create folder for %s: %s", final, exc)
        return False
    temp = _temp_path_for(final)
    try:
        writer(temp)
        if fsync and temp.is_file():
            _fsync_file(temp)
        replace_atomic(temp, final)
        return True
    except (OSError, ValueError) as exc:
        logger.error("Atomic write failed for %s: %s", final, exc)
        safe_unlink(temp)
        return False
    except Exception as exc:  # noqa: BLE001 - writer faults must not escape
        logger.error("Atomic write failed for %s: %s", final, exc)
        safe_unlink(temp)
        return False


def _fsync_file(path: Path) -> None:
    """Best-effort flush of a written file to disk."""
    try:
        handle = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def atomic_write_text(
    path: str | Path,
    text: str,
    encoding: str = "utf-8",
) -> bool:
    """Atomically write text (SRT sidecars, metadata, reports).

    Args:
        path: Destination file.
        text: Full file contents.
        encoding: Text encoding.

    Returns:
        True on success.
    """

    def _write(temp: Path) -> None:
        with temp.open("w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()

    return atomic_write(path, _write)


def atomic_write_bytes(path: str | Path, data: bytes) -> bool:
    """Atomically write binary content.

    Args:
        path: Destination file.
        data: Full file contents.

    Returns:
        True on success.
    """

    def _write(temp: Path) -> None:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()

    return atomic_write(path, _write)


def atomic_write_json(
    path: str | Path,
    data: Any,
    *,
    indent: Optional[int] = 2,
    ensure_ascii: bool = False,
    trailing_newline: bool = False,
) -> bool:
    """Atomically serialize JSON to disk.

    Serialization happens BEFORE the destination is touched, so a value
    that cannot be encoded (a stray set, a NaN-bearing object) leaves the
    existing file intact instead of truncating it.

    Args:
        path: Destination file.
        data: JSON-serializable value.
        indent: json.dump indent.
        ensure_ascii: json.dump ensure_ascii.
        trailing_newline: Append a final newline (config convention).

    Returns:
        True on success.
    """
    try:
        payload = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii)
    except (TypeError, ValueError) as exc:
        logger.error("Refusing to write non-serializable JSON to %s: %s", path, exc)
        return False
    if trailing_newline:
        payload += "\n"
    return atomic_write_text(path, payload)


def read_json(path: str | Path, default: Any = None) -> Any:
    """Read JSON, returning ``default`` for any missing/corrupt file.

    Args:
        path: File to read.
        default: Value returned when the file is unusable.

    Returns:
        Parsed JSON or ``default``.
    """
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.warning("Unreadable JSON at %s: %s", path, exc)
        return default


def quarantine_corrupt_file(path: str | Path) -> Optional[Path]:
    """Move a corrupt file aside so it is not re-read every launch.

    Args:
        path: The unreadable file.

    Returns:
        The quarantine path, or None when nothing was moved.
    """
    source = Path(path)
    if not source.is_file():
        return None
    target = source.with_name(f"{source.name}.corrupt")
    try:
        safe_unlink(target)
        replace_atomic(source, target)
        logger.warning("Quarantined corrupt file %s -> %s", source, target)
        return target
    except OSError as exc:
        logger.warning("Could not quarantine %s: %s", source, exc)
        return None


def purge_stale_temp_files(folder: str | Path, max_age_seconds: float = 86400.0) -> int:
    """Delete this module's leftover temp files from crashed runs.

    Only files carrying :data:`TEMP_MARKER` are considered, so nothing a
    user (or another tool) put in the folder can ever be removed.

    Args:
        folder: Directory to sweep (non-recursive).
        max_age_seconds: Minimum age before a leftover is removed, so a
            concurrent write in progress is never touched.

    Returns:
        Number of files removed.
    """
    root = Path(folder)
    if not root.is_dir():
        return 0
    cutoff = time.time() - max(0.0, max_age_seconds)
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if TEMP_MARKER not in entry.name:
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("Removed %d stale temp file(s) from %s", removed, root)
    return removed


def ensure_directory(path: str | Path) -> Optional[Path]:
    """Create a directory tree, returning None when impossible.

    Args:
        path: Directory to create.

    Returns:
        The directory, or None when it could not be created.
    """
    try:
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError as exc:
        logger.error("Cannot create directory %s: %s", path, exc)
        return None


def writable_directory(
    preferred: str | Path,
    fallback_name: str = "autopilot",
) -> Path:
    """Return a usable directory, degrading to the OS temp area.

    Used by services that must not prevent the app from starting just
    because their configured folder is unavailable (read-only install
    location, a removed network drive, a permission change).

    Args:
        preferred: The configured directory.
        fallback_name: Sub-folder created under the OS temp directory.

    Returns:
        A directory that exists (the preferred one whenever possible).
    """
    created = ensure_directory(preferred)
    if created is not None:
        return created
    import tempfile

    fallback = Path(tempfile.gettempdir()) / fallback_name
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(tempfile.gettempdir())
    logger.warning("Falling back to %s (cannot use %s)", fallback, preferred)
    return fallback


class LazyModule:
    """Lazy module proxy: defers `__import__` until first attribute access."""

    def __init__(self, mod_name: str) -> None:
        self._mod_name = mod_name
        self._val: Optional[Any] = None
        self._tried = False

    def _get(self) -> Any:
        if self._val is None and not self._tried:
            self._tried = True
            try:
                self._val = __import__(self._mod_name)
            except ImportError:
                self._val = None
        return self._val

    def __bool__(self) -> bool:
        return self._get() is not None

    def __getattr__(self, name: str) -> Any:
        mod = self._get()
        if mod is None:
            raise ImportError(f"No module named '{self._mod_name}'")
        return getattr(mod, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        mod = self._get()
        if mod is None:
            raise ImportError(f"No module named '{self._mod_name}'")
        return mod(*args, **kwargs)


class LazyAttribute:
    """Lazy attribute proxy: defers `from mod import attr` until first access."""

    def __init__(self, mod_name: str, attr_name: str) -> None:
        self._mod_name = mod_name
        self._attr_name = attr_name
        self._val: Optional[Any] = None
        self._tried = False

    def _get(self) -> Any:
        if self._val is None and not self._tried:
            self._tried = True
            try:
                mod = __import__(self._mod_name, fromlist=[self._attr_name])
                self._val = getattr(mod, self._attr_name)
            except (ImportError, AttributeError):
                self._val = None
        return self._val

    def __bool__(self) -> bool:
        return self._get() is not None

    def __getattr__(self, name: str) -> Any:
        val = self._get()
        if val is None:
            raise ImportError(
                f"Cannot import '{self._attr_name}' from '{self._mod_name}'"
            )
        return getattr(val, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        val = self._get()
        if val is None:
            raise ImportError(
                f"Cannot import '{self._attr_name}' from '{self._mod_name}'"
            )
        return val(*args, **kwargs)

