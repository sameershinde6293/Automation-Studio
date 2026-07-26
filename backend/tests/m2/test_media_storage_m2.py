"""Milestone 2 secure media storage tests."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from app.core.errors import SecurityError, ValidationError
from app.services.media.storage import (
    delete_file,
    detect_mime,
    media_root,
    resolve_media_path,
    sanitize_filename,
    to_relative,
    write_stream,
)


BAD_PATHS = [
    "../escape.txt",
    "nested/../../escape.txt",
    "/tmp/escape.txt",
    "C:/absolute/on/windows.txt",
    "folder/..",
    "folder/../file.txt",
    "null\x00byte.txt",
] + [f"safe/{'../' * depth}escape-{depth}.txt" for depth in range(1, 121)]


@pytest.mark.parametrize("bad_path", BAD_PATHS)
def test_rejects_path_traversal_absolute_and_null_bytes(tmp_media_root, bad_path):
    with pytest.raises((SecurityError, ValidationError)):
        resolve_media_path(bad_path)


@pytest.mark.parametrize("name", ["a.png", "../a.png", "spaces are ok.jpg", "semi;colon.mp4", "unicode-✓.txt"])
def test_sanitize_filename_removes_directories_and_unsafe_chars(name):
    safe = sanitize_filename(name)
    assert "/" not in safe and "\\" not in safe and safe not in {"", ".", ".."}


def test_resolve_media_path_stays_under_root(tmp_media_root):
    path = resolve_media_path("uploads/file.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok")
    assert path.resolve().is_relative_to(tmp_media_root.resolve())
    assert to_relative(path) == "uploads/file.txt"


def test_symlink_escape_is_blocked(tmp_media_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_media_root / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SecurityError):
        resolve_media_path("link/evil.txt")


@pytest.mark.parametrize(
    "header,filename,expected_mime,media_type",
    [
        (b"\xff\xd8\xff\x00", "photo.jpg", "image/jpeg", "image"),
        (b"\x89PNG\r\n\x1a\n", "photo.png", "image/png", "image"),
        (b"GIF87a", "photo.gif", "image/gif", "image"),
        (b"GIF89a", "photo.gif", "image/gif", "image"),
        (b"RIFFxxxxWEBP", "photo.webp", "image/webp", "image"),
        (b"%PDF-1.7", "doc.pdf", "application/pdf", "document"),
        (b"ID3abc", "song.mp3", "audio/mpeg", "audio"),
        (b"\xff\xfbabc", "song.mp3", "audio/mpeg", "audio"),
        (b"OggSabc", "song.ogg", "audio/ogg", "audio"),
        (b"\x00\x00\x00 ftypmp42", "movie.mp4", "video/mp4", "video"),
    ] * 7,
)
def test_mime_detection_uses_file_content(header, filename, expected_mime, media_type):
    assert detect_mime(header, filename) == (expected_mime, media_type)


def test_stream_upload_writes_and_hashes(tmp_media_root):
    info = write_stream(BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * 10), "../image.png", max_bytes=100)
    assert info["filename"] == "image.png"
    assert info["content_type"] == "image/png"
    assert info["media_type"] == "image"
    assert len(info["checksum_sha256"]) == 64
    assert resolve_media_path(info["file_path"], must_exist=True).exists()


def test_stream_upload_rejects_empty_file(tmp_media_root):
    with pytest.raises(ValidationError):
        write_stream(BytesIO(b""), "empty.bin", max_bytes=100)


def test_stream_upload_enforces_size_during_stream(tmp_media_root):
    with pytest.raises(ValidationError) as exc:
        write_stream(BytesIO(b"x" * 20), "big.bin", max_bytes=10)
    assert exc.value.status_code == 413
    assert not any(tmp_media_root.rglob("big.bin"))


def test_delete_file_only_deletes_under_media_root(tmp_media_root):
    path = resolve_media_path("uploads/delete-me.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("bye")
    delete_file("uploads/delete-me.txt")
    assert not path.exists()
