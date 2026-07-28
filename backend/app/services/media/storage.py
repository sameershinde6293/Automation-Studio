"""Secure media storage helpers constrained to ``settings.MEDIA_ROOT``."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import BinaryIO, Dict, Tuple

from app.core.errors import SecurityError, ValidationError
from app.infrastructure.config.settings import settings

CHUNK_SIZE = 1024 * 1024
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

MAGIC_TYPES = [
    (b"\xff\xd8\xff", "image/jpeg", "image"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "image"),
    (b"GIF87a", "image/gif", "image"),
    (b"GIF89a", "image/gif", "image"),
    (b"RIFF", "image/webp", "image"),  # refined below for WEBP
    (b"%PDF", "application/pdf", "document"),
    (b"ID3", "audio/mpeg", "audio"),
    (b"\xff\xfb", "audio/mpeg", "audio"),
    (b"OggS", "audio/ogg", "audio"),
]

EXTENSION_FALLBACKS = {
    ".mp4": ("video/mp4", "video"),
    ".mov": ("video/quicktime", "video"),
    ".mkv": ("video/x-matroska", "video"),
    ".webm": ("video/webm", "video"),
    ".wav": ("audio/wav", "audio"),
    ".flac": ("audio/flac", "audio"),
    ".txt": ("text/plain", "document"),
    ".json": ("application/json", "document"),
}


def media_root() -> Path:
    root = settings.media_root_path
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_under_root(path: Path) -> Path:
    root = media_root().resolve()
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SecurityError(
            "Media path escapes MEDIA_ROOT.", details={"path": str(path), "root": str(root)}
        ) from exc
    return resolved


def reject_bad_path(raw_path: str) -> None:
    if raw_path is None or raw_path == "":
        raise ValidationError("Media path must be non-empty.")
    if "\x00" in raw_path:
        raise SecurityError("Null-byte paths are not allowed.")
    if len(raw_path) >= 2 and raw_path[1] == ":":
        raise SecurityError("Windows-style absolute media paths are not allowed.")
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise SecurityError("Absolute media paths are not allowed.")
    if any(part == ".." for part in candidate.parts):
        raise SecurityError("Path traversal is not allowed for media paths.")


def resolve_media_path(relative_path: str, *, must_exist: bool = False) -> Path:
    reject_bad_path(relative_path)
    candidate = media_root() / relative_path
    resolved = _ensure_under_root(candidate)
    if must_exist and not resolved.exists():
        raise ValidationError("Media file does not exist.", details={"path": relative_path})
    if must_exist:
        resolved = resolved.resolve(strict=True)
        _ensure_under_root(resolved)
    else:
        # Existing symlinked parent directories must not escape the root.
        parent = resolved.parent
        existing_parent = parent
        while not existing_parent.exists() and existing_parent != media_root():
            existing_parent = existing_parent.parent
        _ensure_under_root(existing_parent.resolve(strict=True))
    return resolved


def to_relative(path: Path) -> str:
    return str(_ensure_under_root(path).relative_to(media_root().resolve()))


def sanitize_filename(filename: str) -> str:
    if not filename or "\x00" in filename:
        raise ValidationError("A valid filename is required.")
    name = Path(filename).name.strip().replace(" ", "_")
    name = _SAFE_NAME_RE.sub("_", name)
    if name in {"", ".", ".."}:
        raise ValidationError("A valid filename is required.")
    return name[:255]


def detect_mime(data: bytes, filename: str = "") -> Tuple[str, str]:
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", "image"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4", "video"
    for magic, mime, media_type in MAGIC_TYPES:
        if data.startswith(magic):
            return mime, media_type
    ext = Path(filename).suffix.lower()
    return EXTENSION_FALLBACKS.get(ext, ("application/octet-stream", "binary"))


def write_stream(stream: BinaryIO, filename: str, *, max_bytes: int | None = None) -> Dict[str, object]:
    safe_name = sanitize_filename(filename)
    max_bytes = settings.MEDIA_MAX_FILE_BYTES if max_bytes is None else max_bytes
    subdir = secrets.token_hex(8)
    rel_path = f"uploads/{subdir}/{safe_name}"
    dest = resolve_media_path(rel_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    first = b""
    # Write beside the destination and publish with replace() so an interrupted
    # upload can never leave a project-visible partial asset.
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=f".{safe_name}.", delete=False) as out:
            temp_path = Path(out.name)
            while True:
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValidationError(
                        "Upload exceeds MEDIA_MAX_FILE_BYTES.",
                        details={"max_bytes": max_bytes}, status_code=413,
                        code="payload_too_large",
                    )
                if len(first) < 512:
                    first += chunk[: 512 - len(first)]
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if size == 0:
            raise ValidationError("Uploaded file is empty.")
        content_type, media_type = detect_mime(first, safe_name)
        if media_type == "image":
            # Magic bytes alone are insufficient: reject truncated/corrupt images.
            try:
                from PIL import Image
                with Image.open(temp_path) as image:
                    image.verify()
            except Exception as exc:
                raise ValidationError("Uploaded image is corrupt or unsupported.") from exc
        os.replace(temp_path, dest)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return {
        "filename": safe_name,
        "file_path": to_relative(dest),
        "media_type": media_type,
        "content_type": content_type,
        "size_bytes": size,
        "checksum_sha256": digest.hexdigest(),
    }


def delete_file(relative_path: str) -> None:
    path = resolve_media_path(relative_path, must_exist=False)
    if path.exists():
        if path.is_dir():
            raise SecurityError("Refusing to delete a directory as a media asset.")
        path.unlink()
