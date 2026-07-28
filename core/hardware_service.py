"""Hardware detection and FFmpeg acceleration helpers.

Cross-platform by design. Windows-specific paths and WMI HWID are
not fully verified on Linux hosts (STATUS: NOT VERIFIED).
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("autopilot.hardware")


class GPUVendor(Enum):
    """Detected GPU vendor category."""

    NVIDIA = "nvidia"
    AMD = "amd"
    INTEL = "intel"
    APPLE = "apple"
    UNKNOWN = "unknown"
    NONE = "none"


class AccelerationMode(Enum):
    """FFmpeg video encoding acceleration mode."""

    NVENC = "nvenc"
    AMF = "amf"
    QSV = "qsv"
    VIDEOTOOLBOX = "videotoolbox"
    SOFTWARE = "software"


class HardwareService:
    """Detects platform, RAM, FFmpeg, and preferred encoder."""

    def __init__(self, ffmpeg_path: str | Path = "engines/ffmpeg/ffmpeg") -> None:
        """Initialize hardware service.

        Args:
            ffmpeg_path: Preferred path to ffmpeg binary (no hard requirement
                for .exe suffix; resolved per platform).
        """
        self._ffmpeg_path_hint = Path(ffmpeg_path)
        self._detected_mode: Optional[AccelerationMode] = None
        self._ffmpeg_resolved: Optional[Path] = None
        self._ffprobe_resolved: Optional[Path] = None
        self.system = platform.system().lower()
        self.machine = platform.machine().lower()

    def get_platform_info(self) -> Dict[str, str]:
        """Return basic platform metadata.

        Returns:
            Dict with system, release, machine, python version.
        """
        return {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        }

    def is_windows(self) -> bool:
        """Return True if running on Windows."""
        return self.system == "windows"

    def is_linux(self) -> bool:
        """Return True if running on Linux."""
        return self.system == "linux"

    def is_macos(self) -> bool:
        """Return True if running on macOS."""
        return self.system == "darwin"

    def resolve_binary_name(self, name: str) -> str:
        """Append .exe on Windows when missing.

        Args:
            name: Binary base name.

        Returns:
            Platform-appropriate binary name.
        """
        if self.is_windows() and not name.lower().endswith(".exe"):
            return f"{name}.exe"
        if not self.is_windows() and name.lower().endswith(".exe"):
            return name[:-4]
        return name

    def find_ffmpeg(self) -> Optional[Path]:
        """Locate ffmpeg executable.

        Search order: configured hint, engines/ffmpeg/, PATH.

        Returns:
            Path or None if not found.
        """
        # PHASE 9: revalidate as a FILE — a cached directory path (the
        # classic Path("") -> cwd trap) would otherwise keep being
        # returned and then fail inside subprocess with a confusing
        # permission error instead of a clean "FFmpeg not found".
        if self._ffmpeg_resolved and self._ffmpeg_resolved.is_file():
            return self._ffmpeg_resolved
        self._ffmpeg_resolved = None

        candidates: List[Path] = []
        hint = self._ffmpeg_path_hint
        candidates.append(hint)
        # Try with/without .exe
        if hint.suffix.lower() == ".exe":
            candidates.append(hint.with_suffix(""))
        else:
            candidates.append(Path(str(hint) + ".exe"))
        candidates.extend(
            [
                Path("engines/ffmpeg/ffmpeg"),
                Path("engines/ffmpeg/ffmpeg.exe"),
                Path("/usr/bin/ffmpeg"),
                Path("/usr/local/bin/ffmpeg"),
            ]
        )
        which = shutil.which("ffmpeg")
        if which:
            candidates.append(Path(which))

        for candidate in candidates:
            # PHASE 9: `is_file()` instead of `exists()` — an empty or
            # directory-valued hint used to pass this check and be handed
            # to subprocess as the executable. `is_file()` also absorbs
            # the OSError a malformed path can raise on Windows.
            if not candidate or not self._is_file(candidate):
                continue
            if os.access(candidate, os.X_OK):
                self._ffmpeg_resolved = candidate
                logger.info("FFmpeg found: %s", candidate)
                return candidate
            # On Windows, existence without X_OK may still be ok
            if self.is_windows():
                self._ffmpeg_resolved = candidate
                return candidate

        logger.warning(
            "FFmpeg not found (STATUS may be NOT VERIFIED in this environment)"
        )
        return None

    @staticmethod
    def _is_file(candidate: Path) -> bool:
        """True when the path is a real file; never raises.

        PHASE 9: a path containing characters the platform rejects (or a
        stale UNC path pointing at a disconnected share) can raise
        OSError from ``is_file()`` — binary discovery must degrade to
        "not this candidate", never abort the caller.
        """
        try:
            return candidate.is_file()
        except OSError:
            return False

    def find_ffprobe(self) -> Optional[Path]:
        """Locate ffprobe executable.

        Returns:
            Path or None.
        """
        if self._ffprobe_resolved and self._ffprobe_resolved.is_file():
            return self._ffprobe_resolved
        self._ffprobe_resolved = None

        ffmpeg = self.find_ffmpeg()
        candidates: List[Path] = []
        if ffmpeg:
            candidates.append(ffmpeg.with_name(self.resolve_binary_name("ffprobe")))
            candidates.append(ffmpeg.parent / "ffprobe")
            candidates.append(ffmpeg.parent / "ffprobe.exe")
        candidates.extend(
            [
                Path("engines/ffmpeg/ffprobe"),
                Path("engines/ffmpeg/ffprobe.exe"),
            ]
        )
        which = shutil.which("ffprobe")
        if which:
            candidates.append(Path(which))

        for candidate in candidates:
            # PHASE 9: same real-file requirement as find_ffmpeg.
            if candidate and self._is_file(candidate):
                self._ffprobe_resolved = candidate
                return candidate
        return None

    def get_available_ram_mb(self) -> float:
        """Return available system RAM in megabytes.

        Returns:
            Available RAM MB, or 0.0 if unavailable.
        """
        try:
            import psutil

            return float(psutil.virtual_memory().available) / 1024 / 1024
        except Exception as exc:  # noqa: BLE001
            logger.warning("RAM detection failed: %s", exc)
            return 0.0

    def get_total_ram_mb(self) -> float:
        """Return total system RAM in megabytes."""
        try:
            import psutil

            return float(psutil.virtual_memory().total) / 1024 / 1024
        except Exception:  # noqa: BLE001
            return 0.0

    def get_process_rss_mb(self) -> float:
        """Return current process resident memory in megabytes."""
        try:
            import psutil

            process = psutil.Process(os.getpid())
            return float(process.memory_info().rss) / 1024 / 1024
        except Exception:  # noqa: BLE001
            return 0.0

    def get_disk_free_mb(self, path: str | Path = ".") -> float:
        """Return free disk space for path's volume in megabytes."""
        try:
            import psutil

            usage = psutil.disk_usage(str(path))
            return float(usage.free) / 1024 / 1024
        except Exception:  # noqa: BLE001
            try:
                usage = shutil.disk_usage(str(path))
                return float(usage.free) / 1024 / 1024
            except OSError:
                return 0.0

    def detect_acceleration(self) -> AccelerationMode:
        """Detect preferred FFmpeg hardware acceleration mode.

        Returns:
            AccelerationMode (SOFTWARE if unsure or FFmpeg missing).
        """
        if self._detected_mode is not None:
            return self._detected_mode

        ffmpeg = self.find_ffmpeg()
        if ffmpeg is None:
            self._detected_mode = AccelerationMode.SOFTWARE
            return self._detected_mode

        try:
            result = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Could not query FFmpeg encoders: %s", exc)
            self._detected_mode = AccelerationMode.SOFTWARE
            return self._detected_mode

        if "h264_nvenc" in output:
            self._detected_mode = AccelerationMode.NVENC
        elif "h264_amf" in output:
            self._detected_mode = AccelerationMode.AMF
        elif "h264_qsv" in output:
            self._detected_mode = AccelerationMode.QSV
        elif "h264_videotoolbox" in output:
            self._detected_mode = AccelerationMode.VIDEOTOOLBOX
        else:
            self._detected_mode = AccelerationMode.SOFTWARE

        logger.info("Acceleration mode: %s", self._detected_mode.value)
        return self._detected_mode

    def get_video_encoder_args(
        self, mode: Optional[AccelerationMode] = None
    ) -> List[str]:
        """Return FFmpeg video codec args for the mode.

        Args:
            mode: Optional mode override; detects if None.

        Returns:
            List of FFmpeg arguments for video encoding.
        """
        selected = mode or self.detect_acceleration()
        mapping = {
            AccelerationMode.NVENC: [
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p4",
                "-rc",
                "vbr",
            ],
            AccelerationMode.AMF: ["-c:v", "h264_amf", "-quality", "balanced"],
            AccelerationMode.QSV: ["-c:v", "h264_qsv", "-preset", "medium"],
            AccelerationMode.VIDEOTOOLBOX: ["-c:v", "h264_videotoolbox"],
            AccelerationMode.SOFTWARE: [
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
            ],
        }
        return list(mapping.get(selected, mapping[AccelerationMode.SOFTWARE]))

    def ffmpeg_available(self) -> bool:
        """Return True if FFmpeg binary is available."""
        return self.find_ffmpeg() is not None
