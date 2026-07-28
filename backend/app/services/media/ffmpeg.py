"""FFmpeg/ffprobe integration with graceful fallback."""
from __future__ import annotations
import json, os, shutil, subprocess, tempfile
from pathlib import Path
from typing import Any, Dict
from PIL import Image
from app.infrastructure.config.settings import settings
from app.services.media.storage import resolve_media_path, to_relative

def tool_available(binary: str) -> bool:
    return bool(binary) and shutil.which(binary) is not None

def ffmpeg_status() -> Dict[str, Any]:
    return {"ffmpeg": settings.FFMPEG_BINARY, "ffprobe": settings.FFPROBE_BINARY,
            "ffmpeg_available": tool_available(settings.FFMPEG_BINARY),
            "ffprobe_available": tool_available(settings.FFPROBE_BINARY)}

def probe_media(relative_path: str) -> Dict[str, Any]:
    path = resolve_media_path(relative_path, must_exist=True); stat = path.stat()
    fallback = {"available": False, "path": relative_path, "size_bytes": stat.st_size}
    if not tool_available(settings.FFPROBE_BINARY):
        fallback["error"] = "ffprobe unavailable"; return fallback
    try:
        completed = subprocess.run([settings.FFPROBE_BINARY, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, timeout=min(settings.FFMPEG_TIMEOUT_SECONDS, 60), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        fallback["error"] = str(exc); return fallback
    if completed.returncode != 0:
        fallback["error"] = completed.stderr[-1000:]; return fallback
    try: data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        fallback["error"] = f"Invalid ffprobe JSON: {exc}"; return fallback
    data["available"] = True; data["size_bytes"] = stat.st_size; return data

def extract_basic_metadata(relative_path: str, media_type: str, content_type: str | None = None) -> Dict[str, Any]:
    path = resolve_media_path(relative_path, must_exist=True)
    metadata = {"storage": {"path": relative_path, "size_bytes": path.stat().st_size}, "content_type": content_type, "ffmpeg": ffmpeg_status()}
    if media_type == "image":
        try:
            with Image.open(path) as img: metadata["image"] = {"width": img.width, "height": img.height, "format": img.format, "mode": img.mode}
        except Exception as exc: metadata["image_error"] = str(exc)
    if media_type in {"video", "audio"}: metadata["probe"] = probe_media(relative_path)
    return metadata

def generate_poster(relative_path: str, media_type: str, *, size: int | None = None) -> Dict[str, Any]:
    path = resolve_media_path(relative_path, must_exist=True); size = settings.MEDIA_THUMBNAIL_SIZE if size is None else size
    poster = resolve_media_path(f"posters/{path.stem}_poster.jpg"); poster.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mktemp(prefix=f".{poster.name}.", dir=poster.parent))
    try:
        if media_type == "image":
            with Image.open(path) as img:
                img.thumbnail((size, size)); img.convert("RGB").save(temp, "JPEG")
            os.replace(temp, poster); return {"poster_path": to_relative(poster), "generated": True, "method": "pillow"}
        if media_type == "video" and tool_available(settings.FFMPEG_BINARY):
            completed = subprocess.run([settings.FFMPEG_BINARY, "-y", "-i", str(path), "-ss", "00:00:01", "-frames:v", "1", "-vf", f"scale='min({size},iw)':-2", str(temp)], capture_output=True, text=True, timeout=min(settings.FFMPEG_TIMEOUT_SECONDS, 120), check=False)
            if completed.returncode == 0 and temp.exists() and temp.stat().st_size > 0:
                os.replace(temp, poster); return {"poster_path": to_relative(poster), "generated": True, "method": "ffmpeg"}
            return {"poster_path": None, "generated": False, "error": completed.stderr[-1000:]}
        return {"poster_path": None, "generated": False, "error": "poster generation unavailable"}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"poster_path": None, "generated": False, "error": str(exc)}
    finally:
        temp.unlink(missing_ok=True)
