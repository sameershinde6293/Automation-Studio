"""Pytest fixtures for Autopilot tests.

Also hosts the cross-platform fake FFmpeg/ffprobe test doubles shared by
the unit tests (see the section near the bottom of this module).
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

import pytest

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# =====================================================================
# Cross-platform fake FFmpeg/ffprobe test doubles
# =====================================================================
#
# Why this exists (Windows hotfix for WinError 193 failures):
#
# * POSIX: an executable bash script works directly via the shebang.
# * Windows: CreateProcess cannot execute scripts directly, so a bash
#   script raised ``[WinError 193] %1 is not a valid Win32 application``.
#   ``.bat`` files are NOT a valid alternative: they require cmd.exe
#   (engines call subprocess with ``shell=False``), and cmd parsing would
#   corrupt ffmpeg-style arguments containing ``&`` (ASS colour codes
#   such as ``&H00BBGGRR``) or ``(``/``)`` (zoompan/between expressions).
#
# Approach:
# * Fake file NAMES stay exactly ``ffmpeg`` / ``ffprobe`` on every
#   platform, so ``HardwareService.find_ffmpeg`` / ``find_ffprobe``
#   resolution (including the sibling-probe lookup) is unchanged.
# * Only the CONTENT differs: bash on POSIX, a sentinel-marked Python
#   script in Windows mode.
# * A test-only shim wraps ``subprocess.run`` / ``subprocess.Popen`` and
#   routes sentinel-marked fakes through ``sys.executable``. Every other
#   subprocess call (real ffmpeg.exe, git, ...) is left untouched.
#
# The Windows code path can be exercised on any OS with:
#   AUTOPILOT_TEST_WINDOWS_FAKES=1 python -m pytest tests
#
# Fake behaviour is controlled via environment variables (set per-test
# with monkeypatch.setenv):
#   FAKE_FFMPEG_LOG    argv log path (falls back to a baked-in default)
#   FAKE_FRAMES        when non-empty, emit 5 "frame=  N fps= 60.0" lines
#                      on stderr (progress-monitor tests)
#   FAKE_NVENC_LISTED  "1" -> h264_nvenc shows up in the -encoders list
#   FAKE_NVENC_BROKEN  "1" -> h264_nvenc tiny-encode fails (rc 1)
#   FAKE_FFMPEG_FAIL   "1" -> generic conversion failure (rc 1)
#   FAKE_PROBE_DURATION  ffprobe-reported duration in seconds (default 8.0)
#   FAKE_PROBE_FAIL    "1" -> ffprobe exits with rc 1

_FAKE_SENTINEL = "# autopilot-test-fake-binary"
_WINDOWS_FAKES_ENV = "AUTOPILOT_TEST_WINDOWS_FAKES"

_FAKE_FFMPEG_BASH = r"""#!/usr/bin/env bash
# Autopilot test double for ffmpeg (POSIX). Mirrors _FAKE_FFMPEG_PY.
LOG="${FAKE_FFMPEG_LOG:-__DEFAULT_LOG__}"
echo "CMD $@" >> "$LOG"
if [[ "$*" == *"-encoders"* ]]; then
  echo " V..... libx264                  H.264 / AVC"
  if [[ "$FAKE_NVENC_LISTED" == "1" ]]; then echo " V..... h264_nvenc               NVIDIA NVENC H.264"; fi
  exit 0
fi
if [[ "$*" == *"h264_nvenc"* ]] && [[ "$FAKE_NVENC_BROKEN" == "1" ]]; then
  echo "Error initializing output stream 0:0 -- nvenc broken" >&2
  exit 1
fi
if [[ -n "$FAKE_FRAMES" ]]; then
  for i in 0 60 120 180 240; do echo "frame=  $i fps= 60.0" >&2; done
fi
if [[ "$FAKE_FFMPEG_FAIL" == "1" ]]; then
  echo "Conversion failed!" >&2
  exit 1
fi
last="${!#}"
# Only treat the last arg as an output file when it looks like a path
# (never for flag-style invocations such as "-version" / "-encoders").
# D.2 additive: .wav outputs get REAL tiny WAV bytes (0.25s silence,
# 48k stereo 16-bit) so downstream WAV consumers (pydub native
# from_file / wave module) behave exactly like with real ffmpeg.
# Payload MUST be b'\x00\x00' * 24000 (=48000B=12000 stereo frames).
# Do NOT write it as (... * 48000 * 2 // 4): bytes // int is a
# TypeError and the fake then silently emits a header-only WAV.
# Everything else keeps the legacy FAKEMP4DATA mp4-shaped payload.
if [[ -n "$last" ]] && [[ "$last" != -* ]]; then
  mkdir -p "$(dirname "$last")" 2>/dev/null || true
  if [[ "$last" == *.wav ]] && command -v python3 >/dev/null 2>&1; then
    python3 -c "import sys, wave; w = wave.open(sys.argv[1], 'wb'); w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000); w.writeframes(b'\\x00\\x00' * 24000); w.close()" "$last"
  else
    echo "FAKEMP4DATA" > "$last"
  fi
fi
exit 0
"""

_FAKE_FFPROBE_BASH = r"""#!/usr/bin/env bash
# Autopilot test double for ffprobe (POSIX). Mirrors _FAKE_FFPROBE_PY.
# Additive D.2 enrichment: full pydub-shaped JSON (index/codec/rate
# fields) so PATH-based consumers (pydub mediainfo) work exactly like
# the real bundled ffprobe. Legacy keys are unchanged.
if [[ "$FAKE_PROBE_FAIL" == "1" ]]; then exit 1; fi
D="${FAKE_PROBE_DURATION:-8.0}"
echo "    Stream #0:0: Video: h264 (High), yuv420p, 1920x1080, 30 fps" >&2
echo "    Stream #0:1: Audio: pcm_s16le, 48000 Hz, stereo, s16 (16 bit)" >&2
echo "{\"format\":{\"duration\":\"$D\",\"size\":\"1000\",\"bit_rate\":\"1000\",\"format_name\":\"mov,mp4,m4a,3gp,3g2,mj2\"},\"streams\":[{\"index\":0,\"codec_name\":\"h264\",\"codec_type\":\"video\",\"width\":1920,\"height\":1080,\"pix_fmt\":\"yuv420p\",\"r_frame_rate\":\"30/1\",\"duration\":\"$D\"},{\"index\":1,\"codec_name\":\"pcm_s16le\",\"codec_type\":\"audio\",\"sample_rate\":\"48000\",\"channels\":2,\"channel_layout\":\"stereo\",\"duration\":\"$D\"}]}"
exit 0
"""

_FAKE_FFMPEG_PY = '''# autopilot-test-fake-binary
"""Autopilot test double for ffmpeg (Windows mode). Mirrors the bash fake."""
import os
import sys
from pathlib import Path

log = os.environ.get("FAKE_FFMPEG_LOG", r"__DEFAULT_LOG__")
args = sys.argv[1:]
with open(log, "a", encoding="utf-8") as handle:
    handle.write("CMD " + " ".join(args) + "\\n")
joined = " ".join(args)
if "-encoders" in joined:
    sys.stdout.write(" V..... libx264                  H.264 / AVC\\n")
    if os.environ.get("FAKE_NVENC_LISTED") == "1":
        sys.stdout.write(" V..... h264_nvenc               NVIDIA NVENC H.264\\n")
    sys.exit(0)
if "h264_nvenc" in joined and os.environ.get("FAKE_NVENC_BROKEN") == "1":
    sys.stderr.write("Error initializing output stream 0:0 -- nvenc broken\\n")
    sys.exit(1)
if os.environ.get("FAKE_FRAMES"):
    for i in (0, 60, 120, 180, 240):
        sys.stderr.write("frame=  %d fps= 60.0\\n" % i)
    sys.stderr.flush()
if os.environ.get("FAKE_FFMPEG_FAIL") == "1":
    sys.stderr.write("Conversion failed!\\n")
    sys.exit(1)
if args:
    last = args[-1]
    # Only treat the last arg as an output file when it looks like a path
    # (never for flag-style invocations such as "-version" / "-encoders").
    if last and not last.startswith("-"):
        try:
            Path(last).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # D.2 additive: real tiny WAV for .wav outputs (mirrors bash
        # fake: 0.25s 48k stereo 16-bit silence) so pydub's native
        # from_file works with zero decoder involvement. Payload MUST
        # be the precomputed b"\\x00\\x00" * 24000 (see bash comment:
        # bytes // int in a "clever" expression is a TypeError and
        # silently leaves a header-only WAV behind).
        if last.lower().endswith(".wav"):
            import wave

            handle = wave.open(last, "wb")
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(b"\\x00\\x00" * 24000)
            handle.close()
        else:
            # write_bytes keeps mp4 payload byte-identical to the
            # bash fake's echo on every platform.
            Path(last).write_bytes(b"FAKEMP4DATA\\n")
sys.exit(0)
'''

_FAKE_FFPROBE_PY = '''# autopilot-test-fake-binary
"""Autopilot test double for ffprobe (Windows mode). Mirrors the bash fake."""
import json
import os
import sys

if os.environ.get("FAKE_PROBE_FAIL") == "1":
    sys.exit(1)
D = os.environ.get("FAKE_PROBE_DURATION", "8.0")
payload = {
    "format": {
        "duration": D,
        "size": "1000",
        "bit_rate": "1000",
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
    },
    "streams": [
        {
            "index": 0,
            "codec_name": "h264",
            "codec_type": "video",
            "width": 1920,
            "height": 1080,
            "pix_fmt": "yuv420p",
            "r_frame_rate": "30/1",
            "duration": D,
        },
        {
            "index": 1,
            "codec_name": "pcm_s16le",
            "codec_type": "audio",
            "sample_rate": "48000",
            "channels": 2,
            "channel_layout": "stereo",
            "duration": D,
        },
    ],
}
# pydub get_extra_info parses stream lines from STDERR (real ffprobe
# behaviour) - emit matching lines for both streams.
sys.stderr.write("    Stream #0:0: Video: h264 (High), yuv420p, 1920x1080, 30 fps\\n")
sys.stderr.write("    Stream #0:1: Audio: pcm_s16le, 48000 Hz, stereo, s16 (16 bit)\\n")
# compact separators keep the legacy "codec_type":"video" shape
sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\\n")
sys.exit(0)
'''


def windows_fake_mode() -> bool:
    """Return True when tests must use Windows-style (Python) fake binaries.

    Active on Windows hosts, or anywhere when the environment variable
    AUTOPILOT_TEST_WINDOWS_FAKES=1 is set (used to verify the exact
    Windows code path on POSIX development machines and CI).
    """
    if platform.system() == "Windows":
        return True
    return os.environ.get(_WINDOWS_FAKES_ENV) == "1"


def _build_fake(
    directory: Union[str, Path],
    name: str,
    bash_body: str,
    py_body: str,
    *,
    log_path: Optional[Union[str, Path]] = None,
    variant: Optional[str] = None,
) -> Path:
    """Write a fake binary file called *name* into *directory*.

    Args:
        directory: Target folder (created when missing).
        name: File name ("ffmpeg" or "ffprobe") - identical on all
            platforms so HardwareService resolution is unaffected.
        bash_body: POSIX script content.
        py_body: Windows-mode Python script content.
        log_path: Baked-in default argv log (FAKE_FFMPEG_LOG wins).
        variant: "bash" / "python" to force a style; default auto-selects
            via windows_fake_mode().

    Returns:
        Path to the written fake.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    chosen = variant or ("python" if windows_fake_mode() else "bash")
    default_log = str(log_path) if log_path else str(directory / "ffmpeg.log")
    template = py_body if chosen == "python" else bash_body
    exe = directory / name
    exe.write_text(
        template.replace("__DEFAULT_LOG__", default_log),
        encoding="utf-8",
        newline="\n",
    )
    # POSIX (including simulated Windows mode on POSIX hosts): make the
    # fake executable so HardwareService.find_ffmpeg's X_OK hint check
    # accepts it. On real Windows there is no X_OK concept to satisfy.
    if platform.system() != "Windows":
        exe.chmod(0o755)
        assert os.access(exe, os.X_OK)
    return exe


def build_fake_ffmpeg(
    directory: Union[str, Path],
    log_path: Optional[Union[str, Path]] = None,
    *,
    variant: Optional[str] = None,
) -> Path:
    """Write a platform-correct fake ffmpeg and return its path."""
    return _build_fake(
        directory,
        "ffmpeg",
        _FAKE_FFMPEG_BASH,
        _FAKE_FFMPEG_PY,
        log_path=log_path,
        variant=variant,
    )


def build_fake_ffprobe(
    directory: Union[str, Path], *, variant: Optional[str] = None
) -> Path:
    """Write a platform-correct fake ffprobe and return its path."""
    return _build_fake(
        directory, "ffprobe", _FAKE_FFPROBE_BASH, _FAKE_FFPROBE_PY, variant=variant
    )


def _is_python_fake(executable: object) -> bool:
    """Return True when *executable* is a sentinel-marked Python fake."""
    try:
        path = Path(os.fspath(executable))  # type: ignore[arg-type]
        if not path.is_file() or path.stat().st_size > 1_000_000:
            return False
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            first_line = handle.readline(200)
        return first_line.rstrip("\r\n") == _FAKE_SENTINEL
    except (OSError, TypeError, ValueError):
        return False


def _rewrite_fake_argv(args: Sequence[object]) -> Sequence[object]:
    """Route a Python fake through sys.executable (idempotent)."""
    if not isinstance(args, (list, tuple)) or not args:
        return args
    if _is_python_fake(args[0]):
        return [sys.executable, str(args[0]), *args[1:]]
    return args


_SHIMS_INSTALLED = False


def _install_fake_subprocess_shims() -> None:
    """Wrap subprocess.run/Popen so Python test doubles execute.

    Only sentinel-marked files are rewritten; every other subprocess call
    is passed through untouched. Idempotent; test-suite use only.
    """
    global _SHIMS_INSTALLED
    if _SHIMS_INSTALLED:
        return
    _SHIMS_INSTALLED = True
    real_run = subprocess.run

    def run_with_fake_support(popenargs, *a, **kw):  # type: ignore[no-untyped-def]
        return real_run(_rewrite_fake_argv(popenargs), *a, **kw)

    class _FakeFriendlyPopen(subprocess.Popen):  # type: ignore[type-arg]
        """Popen that rewrites Python-fake executables.

        The base class is bound (the real Popen) at class-creation time,
        before the module attribute below is replaced.
        """

        def __init__(self, args, *a, **kw):  # type: ignore[no-untyped-def]
            super().__init__(_rewrite_fake_argv(args), *a, **kw)

    subprocess.run = run_with_fake_support  # type: ignore[assignment]
    subprocess.Popen = _FakeFriendlyPopen  # type: ignore[assignment]


if windows_fake_mode():
    _install_fake_subprocess_shims()


@pytest.fixture
def fake_ffmpeg_factory() -> Callable[..., Path]:
    """Return build_fake_ffmpeg(directory, log_path=None)."""
    return build_fake_ffmpeg


@pytest.fixture
def fake_ffprobe_factory() -> Callable[..., Path]:
    """Return build_fake_ffprobe(directory)."""
    return build_fake_ffprobe


# =====================================================================
# Phase A service fixtures
# =====================================================================


@pytest.fixture
def project_root() -> Path:
    """Return Autopilot project root directory."""
    return ROOT


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Return a temporary database file path."""
    return tmp_path / "test_autopilot.db"


@pytest.fixture
def schema_path(project_root: Path) -> Path:
    """Return path to schema.sql."""
    return project_root / "database" / "schema.sql"


@pytest.fixture
def sqlite_db(temp_db_path: Path, schema_path: Path):
    """Create an initialized SQLiteDatabase instance."""
    from core.database_service import SQLiteDatabase

    db = SQLiteDatabase(db_path=temp_db_path, schema_path=schema_path)
    assert db.initialize() is True
    yield db
    db.close()


@pytest.fixture
def database_service(sqlite_db):
    """Create DatabaseService over temp DB."""
    from core.database_service import DatabaseService

    return DatabaseService(sqlite_db)


@pytest.fixture
def event_bus():
    """Fresh EventBus instance."""
    from core.event_bus import EventBus

    return EventBus()


@pytest.fixture
def config_service(project_root: Path):
    """ConfigService pointed at real config folder."""
    from core.config_service import ConfigService

    return ConfigService(config_folder=project_root / "config")


@pytest.fixture
def production_container(project_root: Path, tmp_path: Path):
    """Production-like container with isolated DB/cache/logs under tmp."""
    from core.service_container import ServiceContainer

    container = ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp_path / "autopilot.db"),
            "schema_path": str(project_root / "database" / "schema.sql"),
            "config_folder": str(project_root / "config"),
            "cache_folder": str(tmp_path / "cache"),
            "log_folder": str(tmp_path / "logs"),
            "ffmpeg_path": "ffmpeg",
            "cache_size_mb": 64,
        },
        project_root=project_root,
    )
    yield container
