"""Export engine: render and export the final MP4 (required module).

File 07 MODULE 12. Responsibilities:
- Render each scene image into a video segment (scale -> zoompan -> grade,
  with LUT-opacity blending and dust/scratch overlays wired in via
  multi-input filter_complex graphs — DEBT-B10a).
- Join segments with xfade transitions (grouped in tens for large projects).
- Hardware encoder detection (NVENC/AMF/QSV, tiny-encode verified) and
  software fallback.
- Live progress via 'frame=' parsing + event_bus 'render.progress'.
- Crash recovery through the render_progress table (segment-level resume).
- Subtitle drawtext segmentation plan for >MAX_CHAIN_WORDS videos — DEBT-B11a.
- Output verification via ffprobe (size, streams, duration tolerance).

Modules never import each other (Rule 1): animation/grade/subtitle FILTERS
are passed in as strings by the orchestrator; export only assembles graphs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.safe_io import replace_atomic, safe_unlink
from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

MODULE_NAME = "export_engine"

ProgressCallback = Optional[Callable[[float, float, float], None]]

FFMPEG_TIMEOUT = 3600
ENCODER_PROBE_TIMEOUT = 60
DURATION_TOLERANCE_S = 1.0
XFADE_GROUP_SIZE = 10
DEFAULT_TRANSITION: Dict[str, Any] = {"type": "crossfade", "duration": 0.8}
SEGMENT_SUBTITLE_S = 30.0  # DEBT-B11a subtitle segmentation window

# File 07 EXPORT_PRESETS (config/export_presets.json mirrors these).
EXPORT_PRESETS: Dict[str, Dict[str, Any]] = {
    "youtube_1080p": {
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "video_codec": "libx264",
        "video_bitrate": "8000k",
        "preset": "slow",
        # SIZE (v3.2.6): was 18 (very high quality, correspondingly large
        # files). Measured directly on real detailed content: CRF 18 ->
        # 726KB, CRF 21 -> 299KB (2.4x smaller) for the same 4s test clip,
        # with quality still excellent (21 is commonly considered
        # visually near-lossless for typical viewing). This directly
        # addresses repeated file-size feedback — this is now the
        # standard default; still overridable via export_preset if you
        # want maximum quality (see youtube_1080p_hq) or smaller files
        # still (fast_preview uses 28).
        "crf": 21,
        "audio_codec": "aac",
        # PERFORMANCE (v3.2.3): was 192k. 128k is transparent for spoken
        # narration (this preset's primary use) and meaningfully smaller;
        # the HQ/4K presets below keep 320k for users who explicitly want
        # higher quality (e.g. music-heavy content).
        "audio_bitrate": "128k",
        "audio_sample_rate": 48000,
        "pixel_format": "yuv420p",
        "movflags": "+faststart",
        "format": "mp4",
    },
    "youtube_1080p_hq": {
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "video_codec": "libx264",
        "video_bitrate": "16000k",
        "preset": "slower",
        "crf": 16,
        "audio_codec": "aac",
        "audio_bitrate": "320k",
        "audio_sample_rate": 48000,
        "pixel_format": "yuv420p",
        "movflags": "+faststart",
        "format": "mp4",
    },
    "youtube_4k": {
        "width": 3840,
        "height": 2160,
        "fps": 30,
        "video_codec": "libx265",
        "video_bitrate": "35000k",
        "preset": "slow",
        "crf": 20,
        "audio_codec": "aac",
        "audio_bitrate": "320k",
        "audio_sample_rate": 48000,
        "pixel_format": "yuv420p",
        "movflags": "+faststart",
        "format": "mp4",
    },
    "fast_preview": {
        "width": 854,
        "height": 480,
        "fps": 24,
        "video_codec": "libx264",
        "video_bitrate": "2000k",
        "preset": "ultrafast",
        "crf": 28,
        "audio_codec": "aac",
        "audio_bitrate": "128k",
        "audio_sample_rate": 44100,
        "pixel_format": "yuv420p",
        "movflags": "+faststart",
        "format": "mp4",
    },
}
BUILTIN_DEFAULT = "youtube_1080p"

# Hardware encoder families to probe, in preference order.
H264_HW_CANDIDATES = ["h264_nvenc", "h264_amf", "h264_qsv"]
HEVC_HW_CANDIDATES = ["hevc_nvenc", "hevc_amf", "hevc_qsv"]

_FRAME_RE = re.compile(r"frame=\s*(\d+)")
_FPS_RE = re.compile(r"fps=\s*([\d.]+)")

# PHASE 8 (rendering & export optimization): a grade filtergraph is
# fully described by the string itself, so the lut3d-stripped form only
# needs computing once per distinct graph rather than per scene.
_LUT3D_RE = re.compile(r",?lut3d='[^']*'")

# PHASE 8: joining more than XFADE_GROUP_SIZE segments renders each group
# as an independent ffmpeg job with no shared state, so groups can run
# concurrently — the same treatment scene renders already get in the
# orchestrator. Bounded so a shared hardware encoder isn't saturated.
MAX_PARALLEL_GROUP_JOINS = 3

# One queued group join: (group index, segments, transitions, temp output)
# and the same tuple with that group's join response appended.
_GroupJob = Tuple[int, List[Dict[str, Any]], List[Dict[str, Any]], Path]
_GroupResult = Tuple[
    int, List[Dict[str, Any]], List[Dict[str, Any]], Path, Dict[str, Any]
]


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


class ExportEngine(BaseModule):
    """Render scene segments, join them, and verify the exported MP4."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize export engine and load export preset configuration."""
        super().__init__(container, MODULE_NAME)
        self._presets: Dict[str, Dict[str, Any]] = dict(EXPORT_PRESETS)
        self._default = BUILTIN_DEFAULT
        self._hw_cache: Optional[Dict[str, Any]] = None
        # PHASE 8 (rendering & export optimization): the grade string is
        # identical for every scene of a project, so its LUT-stripped
        # form is memoized instead of re-derived per rendered scene.
        self._lut_strip_cache: Dict[str, str] = {}
        self._load_config()

    def _strip_lut3d(self, grade_filter: str) -> str:
        """LUT-free form of a grade filtergraph (memoized).

        PHASE 8 (rendering & export optimization): identical input, so
        identical output — the regex substitution is done once per
        distinct grade string rather than once per rendered scene.
        """
        cached = self._lut_strip_cache.get(grade_filter)
        if cached is None:
            cached = _LUT3D_RE.sub("", grade_filter)
            self._lut_strip_cache[grade_filter] = cached
        return cached

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------
    def get_available_presets(self) -> Dict[str, Any]:
        """Return the export preset catalog for UI display / validation."""
        started = time.perf_counter()
        catalog = [
            {
                "id": pid,
                "width": p.get("width"),
                "height": p.get("height"),
                "fps": p.get("fps"),
                "video_codec": p.get("video_codec"),
                "video_bitrate": p.get("video_bitrate"),
                "format": p.get("format", "mp4"),
            }
            for pid, p in self._presets.items()
        ]
        return self.make_response(
            True,
            {
                "presets": catalog,
                "count": len(catalog),
                "default_preset": self._default,
            },
            duration_ms=_ms(started),
        )

    def get_export_preset(self, preset_name: Optional[str]) -> Dict[str, Any]:
        """Resolve an export preset by id (default fallback with warning)."""
        started = time.perf_counter()
        warnings: List[str] = []
        key = str(preset_name or "").strip() or self._default
        if key not in self._presets:
            warnings.append(
                f"Unknown export preset '{preset_name}', using '{self._default}'"
            )
            key = self._default
        return self.make_response(
            True,
            {"preset_name": key, "preset": dict(self._presets[key])},
            warnings=warnings,
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Scene segments (File 07 render_scene_to_video)
    # ------------------------------------------------------------------
    def render_scene_to_video(
        self,
        scene: Dict[str, Any],
        animation_filter: str,
        grade_filter: str,
        output_path: str | Path,
        preset: Optional[str] = None,
        grade_extras: Optional[Dict[str, Any]] = None,
        progress_callback: ProgressCallback = None,
    ) -> Dict[str, Any]:
        """Render a single scene image to a video segment (no audio).

        Filter order (File 07): scale -> zoompan animation -> color grade.
        grade_extras (DEBT-B10a wiring, all optional):
          lut_path/lut_opacity -> split/lut3d/blend normalization graph
          dust_overlay/scratch_overlay (+ opacities) -> extra -i inputs
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="export_engine is disabled")
        # Hotfix (3.1.0 render blocker): an empty image path became
        # Path("") == "." — and Path(".").exists() is True, so FFmpeg
        # was invoked as `-i .` and died "Permission denied". Resolve
        # every candidate key, log it (RULE 4), require a real FILE.
        raw_image = str(
            scene.get("image_path") or scene.get("image")
            or scene.get("image_file_path") or ""
        ).strip()
        scene_no = scene.get("scene_number") or "?"
        if not raw_image:
            return self.make_response(
                False,
                error=(
                    f"scene {scene_no}: image path is empty — the "
                    "images stage did not attach a file (check the "
                    "script's image names against the images folder)"
                ),
                duration_ms=_ms(started),
            )
        image = Path(raw_image)
        self.log.info("Scene %s image path: %s", scene_no, image)
        if not image.is_file():
            return self.make_response(
                False, error=f"Scene image not found: {raw_image}",
                duration_ms=_ms(started),
            )

        pdata = self.get_export_preset(preset)["data"]
        p = pdata["preset"]
        ffmpeg = self._require_ffmpeg()
        if not ffmpeg:
            return self.make_response(
                False,
                error="FFmpeg not available — cannot render scene",
                duration_ms=_ms(started),
            )
        duration = max(0.1, float(scene.get("duration") or 8.0))
        fps, width, height = int(p["fps"]), int(p["width"]), int(p["height"])
        codec_response = self.get_encoder_for_preset(
            p, self.detect_hardware_acceleration()["data"]
        )
        codec = codec_response["data"]["codec"]

        # PERFORMANCE (v3.2.1): eq/colorbalance/vignette/noise/unsharp are
        # STATIC filters — identical math on every single output frame of
        # a scene. Previously they ran inside the same per-frame pipeline
        # as the zoompan animation, so a 2-minute scene at 30fps recomputed
        # the same color grade 3,600 times. For scenes with no LUT/overlay
        # extras (the common case), we now bake the grade onto the source
        # image ONCE (a single-frame ffmpeg pass) and animate the already-
        # graded image — the per-frame pass then only does scale+zoompan.
        # Falls back to the original single-pass behavior on any failure,
        # or when LUT/dust/scratch extras are requested (those need the
        # full per-frame graph).
        render_image = image
        prebaked_path: Optional[Path] = None
        can_prebake = bool(grade_filter) and not (grade_extras or {}).get(
            "lut_path"
        ) and not (grade_extras or {}).get("dust_overlay") and not (
            grade_extras or {}
        ).get("scratch_overlay")
        if can_prebake:
            prebaked_path = Path(output_path).with_suffix(".graded.png")
            baked = self._prebake_grade(
                image, grade_filter, width, height, prebaked_path
            )
            if baked:
                render_image = prebaked_path
            else:
                prebaked_path = None  # fall through to full per-frame path

        graph, extra_inputs, out_label = self._build_scene_graph(
            width,
            height,
            str(animation_filter or ""),
            "" if prebaked_path else str(grade_filter or ""),
            grade_extras or {},
        )
        total_frames = max(1, int(duration * fps))
        command = [ffmpeg, "-y", "-loop", "1", "-i", str(render_image)]
        for overlay in extra_inputs:
            command += ["-loop", "1", "-i", overlay]
        command += ["-t", f"{duration:.3f}"]
        if extra_inputs or "split" in graph:
            command += ["-filter_complex", graph, "-map", f"[{out_label}]"]
        else:
            command += ["-vf", graph]
        command += self._codec_args(codec, p)
        command += [
            "-r",
            str(fps),
            "-s",
            f"{width}x{height}",
            "-pix_fmt",
            str(p["pixel_format"]),
            "-an",
            str(output_path),
        ]

        run = self._run_ffmpeg(command, total_frames, progress_callback, started)
        if prebaked_path is not None:
            safe_unlink(prebaked_path)
        if not run["success"]:
            # PHASE 9: a failed scene render can leave a zero-byte or
            # partially-written segment. The resume logic in the
            # orchestrator probes existing segments and reuses any whose
            # duration matches — a truncated leftover must not be
            # mistaken for finished work on the next attempt.
            self._discard_partial_output(output_path)
            return run
        return self.make_response(
            True,
            {
                "output_path": str(output_path),
                "duration_seconds": duration,
                "total_frames": total_frames,
                "codec": codec,
                "graph_used": graph,
                "grade_prebaked": prebaked_path is not None,
            },
            warnings=list(pdata.get("warnings") or []),
            duration_ms=_ms(started),
        )

    def _prebake_grade(
        self,
        image: Path,
        grade_filter: str,
        width: int,
        height: int,
        output_path: Path,
    ) -> bool:
        """Bake static color-grade filters onto a still image once.

        Returns True on success (output_path now holds the graded image),
        False on any failure — caller falls back to the full per-frame
        grade pass, so this is purely a speed optimization, never a
        correctness requirement.
        """
        try:
            cleaned = self._strip_lut3d(grade_filter)
            if not cleaned:
                return False
            ffmpeg = self._require_ffmpeg()
            if not ffmpeg:
                return False
            command = [
                str(ffmpeg),
                "-y",
                "-nostdin",
                "-i",
                str(image),
                "-vf",
                f"scale={width}:{height},{cleaned}",
                "-frames:v",
                "1",
                # PHASE 8 (rendering & export optimization): PNG is
                # lossless at every compression level, so the graded
                # pixels the encoder reads back are identical — level 1
                # just stops the intermediate spending most of its time
                # deflating a full-HD still that is deleted seconds
                # later.
                "-compression_level",
                "1",
                str(output_path),
            ]
            proc = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
            return proc.returncode == 0 and output_path.exists()
        except (OSError, subprocess.SubprocessError):
            return False



    def render_title_card(
        self,
        title_text: str,
        style: str,
        duration: float,
        output_path: str | Path,
        preset: Optional[str] = None,
        progress_callback: ProgressCallback = None,
    ) -> Dict[str, Any]:
        """Render a title card image and convert it to a video segment."""
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="export_engine is disabled")
        p = self.get_export_preset(preset)["data"]["preset"]
        width, height = int(p["width"]), int(p["height"])

        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return self.make_response(
                False, error="Pillow not installed", duration_ms=_ms(started)
            )
        card = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(card)
        lines = self._wrap_title(str(title_text or ""), 60)
        line_height = 40
        top = (height - line_height * len(lines)) // 2
        for i, line in enumerate(lines):
            box = draw.textbbox((0, 0), line)
            draw.text(
                ((width - (box[2] - box[0])) // 2, top + i * line_height),
                line,
                fill=(235, 235, 235),
            )
        card_path = Path(output_path).with_suffix(".title_card.png")
        card_path.parent.mkdir(parents=True, exist_ok=True)
        card.save(card_path)

        total_frames = max(1, int(float(duration) * int(p["fps"])))
        static = (
            f"zoompan=z='1':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={total_frames}:s={width}x{height}:fps={int(p['fps'])}"
        )
        result = self.render_scene_to_video(
            {"image_path": str(card_path), "duration": duration},
            static,
            "",
            output_path,
            preset=preset,
            progress_callback=progress_callback,
        )
        if not result["success"]:
            return result
        result["data"]["title_card_image"] = str(card_path)
        result["data"]["style"] = style
        result["duration_ms"] = _ms(started)
        return result

    # ------------------------------------------------------------------
    # Segment joining (File 07 join_segments_with_transitions)
    # ------------------------------------------------------------------
    def join_segments_with_transitions(
        self,
        segment_list: List[Any],
        timeline: Dict[str, Any],
        output_path: str | Path,
        preset: Optional[str] = None,
        progress_callback: ProgressCallback = None,
    ) -> Dict[str, Any]:
        """Join scene segments with xfade transitions into one video.

        <10 segments: single xfade filter_complex. >=10: groups of
        XFADE_GROUP_SIZE joined first, then groups joined (transitions
        inside groups from timeline; between groups = default crossfade).
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="export_engine is disabled")
        segments = self._normalize_segments(segment_list)
        for seg in segments:
            if not seg["path"].exists():
                return self.make_response(
                    False, error=f"Segment file not found: {seg['path']}"
                )
        if not segments:
            return self.make_response(False, error="No segments to join")
        if not self._require_ffmpeg():
            return self.make_response(
                False,
                error="FFmpeg not available — cannot join segments",
                duration_ms=_ms(started),
            )

        p = self.get_export_preset(preset)["data"]["preset"]
        transitions = list((timeline or {}).get("transitions") or [])
        join = self._join_group(
            segments, transitions, p, Path(output_path), progress_callback, started
        )
        if not join["success"]:
            return join

        audio_path = (timeline or {}).get("audio_path")
        audio_delay_ms = int((timeline or {}).get("audio_delay_ms") or 0)
        if audio_path and Path(str(audio_path)).exists():
            muxed = self._mux_audio(
                Path(output_path),
                Path(str(audio_path)),
                p,
                progress_callback,
                started,
                audio_delay_ms=audio_delay_ms,
            )
            if not muxed["success"]:
                return muxed
            join["data"]["audio_muxed"] = True
        else:
            if audio_path:
                self.log.warning(
                    "Audio path missing, video left silent: %s", audio_path
                )
            join["data"]["audio_muxed"] = False

        join["data"]["output_path"] = str(output_path)
        join["duration_ms"] = _ms(started)
        return join

    # ------------------------------------------------------------------
    # Hardware encoding (File 07 detect_hardware_acceleration)
    # ------------------------------------------------------------------
    def detect_hardware_acceleration(self, refresh: bool = False) -> Dict[str, Any]:
        """Detect a working hardware encoder; tiny-encode verified.

        Result is cached on the instance (pass refresh=True to re-probe).
        """
        started = time.perf_counter()
        if self._hw_cache is not None and not refresh:
            cached = dict(self._hw_cache)
            return self.make_response(True, cached, duration_ms=_ms(started))

        ffmpeg = self.hardware.find_ffmpeg() if self.hardware else None
        tested: List[str] = []
        encoder: Optional[str] = None
        if ffmpeg:
            try:
                probe = subprocess.run(
                    [str(ffmpeg), "-hide_banner", "-encoders"],
                    capture_output=True,
                    text=True,
                    timeout=ENCODER_PROBE_TIMEOUT,
                )
                listed = (probe.stdout or "") + (probe.stderr or "")
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.log.warning("Encoder probe failed: %s", exc)
                listed = ""
            for candidate in H264_HW_CANDIDATES:
                if candidate in listed:
                    tested.append(candidate)
                    if self._test_encoder(str(ffmpeg), candidate):
                        encoder = candidate
                        break
        hardware = {
            "hardware": encoder is not None,
            "encoder": encoder,
            "tested_candidates": tested,
            "fallback": None if encoder else "software",
        }
        self._hw_cache = hardware
        self.log.info("Hardware acceleration: %s", encoder or "software encoding")
        return self.make_response(True, dict(hardware), duration_ms=_ms(started))

    def get_encoder_for_preset(
        self, preset: Dict[str, Any], hardware_encoder: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Map a preset codec to the best available encoder (File 07 rules)."""
        started = time.perf_counter()
        base = str((preset or {}).get("video_codec") or "libx264")
        codec = base
        used_hw = False
        hw = hardware_encoder or {}
        if hw.get("hardware") and hw.get("encoder"):
            if base == "libx264":
                codec = str(hw["encoder"])
                used_hw = True
            elif base == "libx265" and str(hw["encoder"]).startswith("hevc"):
                codec = str(hw["encoder"])
                used_hw = True
        return self.make_response(
            True,
            {"codec": codec, "base_codec": base, "hardware_accelerated": used_hw},
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Progress monitoring (File 07 monitor_ffmpeg_progress)
    # ------------------------------------------------------------------
    def monitor_ffmpeg_progress(
        self,
        process: subprocess.Popen,
        total_frames: int,
        callback: Callable[[float, float, float], None],
    ) -> None:
        """Parse FFmpeg stderr for 'frame=/fps=' and report via callback.

        Publishes 'render.progress' on the event bus (throttled to percent
        changes) so future UI can subscribe. Returns None (File 07).
        """
        last_percent = -1
        for raw_line in process.stderr or []:
            line = str(raw_line).replace("\r", "\n")
            for chunk in line.splitlines():
                if "error" in chunk.lower() and "frame=" not in chunk:
                    self.log.error("FFmpeg: %s", chunk.strip()[:300])
                frame_match = _FRAME_RE.search(chunk)
                if not frame_match:
                    continue
                frame = int(frame_match.group(1))
                fps_match = _FPS_RE.search(chunk)
                fps = float(fps_match.group(1)) if fps_match else 0.0
                progress = min(100.0, frame / max(1, total_frames) * 100.0)
                eta = (total_frames - frame) / fps if fps > 0 else 0.0
                callback(progress, fps, max(0.0, eta))
                percent_int = int(progress)
                if percent_int != last_percent:
                    last_percent = percent_int
                    self._publish_progress(
                        {"percent": progress, "fps": fps, "eta_seconds": eta}
                    )

    # ------------------------------------------------------------------
    # Estimates (File 07 estimate_render_time / estimate_output_size)
    # ------------------------------------------------------------------
    def estimate_render_time(
        self,
        timeline: Dict[str, Any],
        preset: Optional[str] = None,
        hardware_available: bool = False,
    ) -> Dict[str, Any]:
        """Estimate total render time (60 fps hardware / 15 fps software)."""
        started = time.perf_counter()
        p = self.get_export_preset(preset)["data"]["preset"]
        total = self._timeline_duration(timeline)
        total_frames = int(total * int(p["fps"]))
        assumed_fps = 60 if hardware_available else 15
        seconds = int(total_frames / assumed_fps) + 1
        return self.make_response(
            True,
            {
                "estimated_seconds": seconds,
                "total_frames": total_frames,
                "assumed_fps": assumed_fps,
                "human": self._human_seconds(seconds),
            },
            duration_ms=_ms(started),
        )

    def estimate_output_size(
        self, timeline: Dict[str, Any], preset: Optional[str] = None
    ) -> Dict[str, Any]:
        """Estimate output file size from bitrates and duration (File 07)."""
        started = time.perf_counter()
        p = self.get_export_preset(preset)["data"]["preset"]
        seconds = self._timeline_duration(timeline)
        video_kbps = int(str(p["video_bitrate"]).replace("k", "").replace("M", "000"))
        audio_kbps = int(str(p["audio_bitrate"]).replace("k", "").replace("M", "000"))
        size_bytes = int((video_kbps + audio_kbps) * 1000 * seconds / 8)
        human = (
            f"{size_bytes / (1024 ** 3):.1f} GB"
            if size_bytes >= 1024**3
            else f"{size_bytes / (1024 ** 2):.1f} MB"
        )
        return self.make_response(
            True,
            {
                "size_bytes": size_bytes,
                "size_human": human,
                "duration_seconds": round(seconds, 1),
                "total_bitrate_kbps": video_kbps + audio_kbps,
            },
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Output verification (File 07 verify_output)
    # ------------------------------------------------------------------
    def export_vertical_cut(
        self,
        video_path: str | Path,
        output_path: str | Path,
        progress_callback: ProgressCallback = None,
    ) -> Dict[str, Any]:
        """Export a 9:16 vertical cut of a finished 16:9 video (v3.2.4).

        Center-crop to 9:16, keeping full height and cropping the sides
        evenly. This is a straightforward crop, not focal-point-aware
        (doesn't know which part of each scene is the "important" part) —
        for content that's already reasonably centered (typical for this
        app's zoompan-animated scenes, which pan toward center by default)
        this looks correct; a genuinely focal-point-aware crop would need
        per-scene metadata this app doesn't currently track.
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="export_engine is disabled")
        src = Path(video_path)
        if not src.is_file():
            return self.make_response(
                False, error=f"Video not found: {src}", duration_ms=_ms(started)
            )
        ffmpeg = self._require_ffmpeg()
        if not ffmpeg:
            return self.make_response(
                False, error="FFmpeg not available", duration_ms=_ms(started)
            )
        codec_response = self.get_encoder_for_preset(
            {"crf": 20}, self.detect_hardware_acceleration()["data"]
        )
        codec = codec_response["data"]["codec"]
        # Crop to a 9:16 window: full height, width = height*9/16, centered.
        vf = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920"
        command = [
            str(ffmpeg), "-y", "-i", str(src), "-vf", vf,
            *self._codec_args(codec, {"crf": 20, "preset": "medium"}),
            "-c:a", "copy", str(output_path),
        ]
        run = self._run_ffmpeg(command, 0, progress_callback, started)
        if not run["success"]:
            return run
        return self.make_response(
            True,
            {"output_path": str(output_path), "aspect": "9:16", "codec": codec},
            duration_ms=_ms(started),
        )

    def export_chaptered_parts(
        self,
        video_path: str | Path,
        chapters: List[Dict[str, Any]],
        output_dir: str | Path,
        base_name: str = "part",
    ) -> Dict[str, Any]:
        """Split a finished video into chaptered parts via stream copy (v3.2.4).

        Uses -c copy (no re-encode) at each chapter boundary — near-instant
        regardless of video length, since it's just repackaging existing
        encoded data, not re-compressing. Useful for platforms/storage
        that don't handle one huge multi-hour file well. `chapters` is the
        same list this app already generates for YouTube chapter markers
        (each needs a "start_time" in seconds); if fewer than 2 chapters
        are given, nothing is split (a single "part" would be pointless).
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="export_engine is disabled")
        src = Path(video_path)
        if not src.is_file():
            return self.make_response(
                False, error=f"Video not found: {src}", duration_ms=_ms(started)
            )
        if len(chapters) < 2:
            return self.make_response(
                True,
                {"parts": [], "skipped": "fewer than 2 chapters — nothing to split"},
                duration_ms=_ms(started),
            )
        ffmpeg = self._require_ffmpeg()
        if not ffmpeg:
            return self.make_response(
                False, error="FFmpeg not available", duration_ms=_ms(started)
            )
        total_duration = self._probe_path_duration(src) or 0.0
        starts = sorted(float(c.get("start_time") or 0.0) for c in chapters)
        bounds = starts + [total_duration]

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        parts: List[str] = []
        for i in range(len(bounds) - 1):
            begin, end = bounds[i], bounds[i + 1]
            if end <= begin:
                continue
            part_path = out_dir / f"{base_name}_{i + 1:02d}.mp4"
            command = [
                str(ffmpeg), "-y", "-ss", f"{begin:.3f}", "-i", str(src),
                "-t", f"{end - begin:.3f}", "-c", "copy",
                "-movflags", "+faststart", str(part_path),
            ]
            run = self._run_ffmpeg(command, 0, None, started)
            if not run["success"]:
                return self.make_response(
                    False,
                    error=f"part {i + 1} failed: {run.get('error')}",
                    data={"parts_completed": parts},
                    duration_ms=_ms(started),
                )
            parts.append(str(part_path))
        return self.make_response(
            True, {"parts": parts, "count": len(parts)}, duration_ms=_ms(started)
        )

    def verify_output(
        self, output_path: str | Path, expected_duration: float
    ) -> Dict[str, Any]:
        """Verify the rendered MP4: exists, non-empty, streams, duration."""
        started = time.perf_counter()
        path = Path(output_path)
        issues: List[str] = []
        if not path.exists():
            return self.make_response(
                False, error=f"Output not found: {path}", duration_ms=_ms(started)
            )
        size = path.stat().st_size
        if size <= 0:
            issues.append("File is empty (0 bytes)")

        ffprobe = self.hardware.find_ffprobe() if self.hardware else None
        if not ffprobe:
            return self.make_response(
                False,
                error="ffprobe not available — cannot verify output",
                duration_ms=_ms(started),
            )
        try:
            probe = subprocess.run(
                [
                    str(ffprobe),
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            data = json.loads(probe.stdout or "{}") if probe.returncode == 0 else {}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            self.log.error("ffprobe failed: %s", exc)
            data = {}
        if not data:
            issues.append("ffprobe returned no usable metadata")

        streams = data.get("streams") or []
        has_video = any(s.get("codec_type") == "video" for s in streams)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        if not has_video:
            issues.append("No video stream found")
        if not has_audio:
            issues.append("No audio stream found")
        actual = self._probe_duration(data)
        if (
            actual is not None
            and abs(actual - float(expected_duration)) > DURATION_TOLERANCE_S
        ):
            issues.append(
                f"Duration mismatch: expected {expected_duration:.2f}s, got {actual:.2f}s"
            )
        valid = (
            size > 0
            and has_video
            and has_audio
            and not any("mismatch" in i or "metadata" in i for i in issues)
        )
        return self.make_response(
            True,
            {
                "valid": bool(valid),
                "actual_duration": actual,
                "expected_duration": float(expected_duration),
                "has_video": has_video,
                "has_audio": has_audio,
                "size_bytes": size,
                "issues": issues,
            },
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Crash recovery (render_progress table) + DEBT-B11a segmentation
    # ------------------------------------------------------------------
    def create_render_plan(
        self,
        project_id: str,
        scenes: List[Dict[str, Any]],
        temp_dir: str | Path,
        settings_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a resumable render plan for a project (30s-friendly segments)."""
        started = time.perf_counter()
        session_id = str(uuid.uuid4())
        plan = [
            {
                "scene_id": str(scene.get("id") or scene.get("scene_id") or i),
                "segment_path": str(Path(temp_dir) / f"segment_{i:04d}.mp4"),
                "status": "pending",
            }
            for i, scene in enumerate(scenes)
        ]
        now = utc_now_str()
        db = getattr(self.db, "db", self.db)
        try:
            db.execute(
                "DELETE FROM render_progress WHERE project_id = ?", (str(project_id),)
            )
            db.execute(
                "INSERT INTO render_progress (id, project_id, render_session_id,"
                " current_stage, stage_percent, current_scene_number, total_scenes,"
                " completed_scenes_json, failed_scenes_json, segment_files_json,"
                " started_at, updated_at, render_settings_json, error_count, is_resumable)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    str(project_id),
                    session_id,
                    "rendering",
                    0.0,
                    0,
                    len(plan),
                    "[]",
                    "[]",
                    json.dumps([s["segment_path"] for s in plan]),
                    now,
                    now,
                    json.dumps({"segments": plan, "settings": settings_snapshot or {}}),
                    0,
                    1,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self.log.error("Failed to create render plan: %s", exc)
            return self.make_response(False, error=str(exc), duration_ms=_ms(started))
        self._publish_progress(
            {"stage": "rendering", "percent": 0.0, "session_id": session_id}
        )
        return self.make_response(
            True,
            {"session_id": session_id, "segments": plan, "total_segments": len(plan)},
            duration_ms=_ms(started),
        )

    def update_segment_status(
        self,
        project_id: str,
        scene_id: str,
        status: str,
        segment_path: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Mark a segment completed/failed and refresh progress percent."""
        started = time.perf_counter()
        state = self.get_resume_state(project_id)
        if not state["success"]:
            return state
        row = state["data"]
        completed = set(row["completed_scenes"])
        failed = set(row["failed_scenes"])
        if status == "completed":
            completed.discard(str(scene_id))
            completed.add(str(scene_id))
            failed.discard(str(scene_id))
        elif status == "failed":
            failed.add(str(scene_id))
        else:
            return self.make_response(False, error=f"Unknown status '{status}'")
        total = int(row["total_scenes"]) or 1
        percent = round(len(completed) / total * 100.0, 1)
        db = getattr(self.db, "db", self.db)
        try:
            db.execute(
                "UPDATE render_progress SET current_stage = ?, stage_percent = ?,"
                " current_scene_id = ?, completed_scenes_json = ?, failed_scenes_json = ?,"
                " segment_files_json = ?, updated_at = ?, error_count = ?, last_error = ?"
                " WHERE project_id = ?",
                (
                    "rendering",
                    percent,
                    str(scene_id),
                    json.dumps(sorted(completed)),
                    json.dumps(sorted(failed)),
                    json.dumps(row["planned_segment_files"]),
                    utc_now_str(),
                    int(row["error_count"]) + (1 if status == "failed" else 0),
                    error if status == "failed" else row.get("last_error"),
                    str(project_id),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return self.make_response(False, error=str(exc), duration_ms=_ms(started))
        self._publish_progress(
            {
                "stage": "rendering",
                "percent": percent,
                "scene_id": str(scene_id),
                "status": status,
            }
        )
        return self.make_response(
            True,
            {"completed": len(completed), "failed": len(failed), "percent": percent},
            duration_ms=_ms(started),
        )

    def get_resume_state(self, project_id: str) -> Dict[str, Any]:
        """Read the resume checkpoint: completed/pending scenes, segments on disk."""
        started = time.perf_counter()
        db = getattr(self.db, "db", self.db)
        try:
            row = db.fetch_one(
                "SELECT * FROM render_progress WHERE project_id = ?", (str(project_id),)
            )
        except Exception as exc:  # noqa: BLE001
            return self.make_response(False, error=str(exc), duration_ms=_ms(started))
        if not row:
            return self.make_response(
                False, error=f"No render plan for project '{project_id}'"
            )
        # PHASE 9 (corrupt checkpoint recovery): a render_progress row
        # whose JSON columns were truncated by a hard crash used to
        # raise JSONDecodeError out of this method — the crash-recovery
        # dialog then failed with a raw parser error and the project
        # could not be resumed OR restarted from the UI. Each column
        # now degrades to its empty default independently, so a partly
        # damaged checkpoint still yields whatever is still readable.
        completed = set(self._json_column(row, "completed_scenes_json", []))
        failed = set(self._json_column(row, "failed_scenes_json", []))
        settings = self._json_column(row, "render_settings_json", {})
        if not isinstance(settings, dict):
            settings = {}
        planned = settings.get("segments") or []
        pending = [
            s for s in planned
            if isinstance(s, dict) and s.get("scene_id") not in completed
        ]
        planned_files = self._json_column(row, "segment_files_json", [])
        disk_segments = [p for p in planned_files if p and Path(str(p)).is_file()]
        return self.make_response(
            True,
            {
                "session_id": row.get("render_session_id", ""),
                "stage": row.get("current_stage", ""),
                "completed_scenes": sorted(completed),
                "failed_scenes": sorted(failed),
                "pending_scenes": pending,
                "segment_files": disk_segments,
                "planned_segment_files": planned_files,
                "total_scenes": int(row.get("total_scenes") or len(planned)),
                "error_count": int(row.get("error_count") or 0),
                "last_error": row.get("last_error"),
                "resumable": bool(row.get("is_resumable", 1)),
                "settings": settings.get("settings") or {},
            },
            duration_ms=_ms(started),
        )

    def _json_column(self, row: Dict[str, Any], column: str, default: Any) -> Any:
        """Parse one JSON DB column, falling back to ``default``.

        PHASE 9: checkpoint columns are written by a process that can be
        killed at any moment; one unreadable column must not make the
        whole checkpoint unreadable.
        """
        raw = row.get(column)
        if not raw:
            return default
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            self.log.warning("Corrupt %s in render_progress: %s", column, exc)
            return default
        return value if isinstance(value, type(default)) else default

    def finish_render_state(
        self, project_id: str, success: bool, error: Optional[str] = None
    ) -> Dict[str, Any]:
        """Close the render plan (completed / failed) with final percent."""
        started = time.perf_counter()
        stage = "completed" if success else "failed"
        db = getattr(self.db, "db", self.db)
        try:
            db.execute(
                "UPDATE render_progress SET current_stage = ?, stage_percent = ?,"
                " updated_at = ?, last_error = ?, is_resumable = ? WHERE project_id = ?",
                (
                    stage,
                    100.0 if success else -1.0,
                    utc_now_str(),
                    error,
                    0 if success else 1,
                    str(project_id),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return self.make_response(False, error=str(exc), duration_ms=_ms(started))
        self._publish_progress(
            {"stage": stage, "percent": 100.0 if success else -1.0, "error": error}
        )
        return self.make_response(True, {"stage": stage}, duration_ms=_ms(started))

    def plan_subtitle_segments(
        self, words: List[Dict[str, Any]], segment_seconds: float = SEGMENT_SUBTITLE_S
    ) -> Dict[str, Any]:
        """DEBT-B11a: split word timestamps into time-windowed chunks so each
        drawtext chain stays within subtitle_engine's single-pass limit.

        The orchestrator applies each window to its corresponding scene
        segment render via subtitle_engine (passed as filter strings here).
        """
        started = time.perf_counter()
        if not words:
            return self.make_response(
                False, error="No words supplied", duration_ms=_ms(started)
            )
        window = max(1.0, float(segment_seconds))
        segments: List[Dict[str, Any]] = []
        current: List[Dict[str, Any]] = []
        window_start = self._as_float(words[0].get("start_time_ms"), 0.0) / 1000.0
        for word in words:
            start_s = self._as_float(word.get("start_time_ms"), 0.0) / 1000.0
            if start_s - window_start >= window and current:
                segments.append(self._subtitle_window(window_start, start_s, current))
                window_start, current = start_s, []
            current.append(word)
        if current:
            end_s = (
                self._as_float(current[-1].get("end_time_ms"), window_start * 1000.0)
                / 1000.0
            )
            segments.append(self._subtitle_window(window_start, end_s, current))
        max_words = max((len(s["words"]) for s in segments), default=0)
        return self.make_response(
            True,
            {
                "segments": segments,
                "count": len(segments),
                "max_words_per_segment": max_words,
                "window_seconds": window,
            },
            duration_ms=_ms(started),
        )

    def is_optional_module(self) -> bool:
        """Return False — export_engine is required (File 07: CAN BE DISABLED NO)."""
        return False

    # ------------------------------------------------------------------
    # Graph construction internals
    # ------------------------------------------------------------------
    def _build_scene_graph(
        self,
        width: int,
        height: int,
        animation_filter: str,
        grade_filter: str,
        extras: Dict[str, Any],
    ) -> Tuple[str, List[str], str]:
        """Assemble the scene filtergraph + overlay inputs (DEBT-B10a).

        Returns (graph, extra_input_paths, output_label). LUT blending:
        grade chain runs first, then [base]split -> lut3d -> blend with
        all_opacity. Dust/scratch arrive as extra inputs and overlay on top.
        """
        chain = f"scale={width}:{height}"
        if animation_filter:
            chain += f",{animation_filter}"
        # Strip any lut3d already embedded by the grade builder — the LUT is
        # re-applied here, blended at grade_extras opacity (single authority).
        if grade_filter:
            cleaned = self._strip_lut3d(grade_filter)
            if cleaned:
                chain += f",{cleaned}"

        label = "v0"
        steps = [f"[0:v]{chain}[{label}]"]
        lut_path = extras.get("lut_path")
        if lut_path:
            if not Path(str(lut_path)).exists():
                self.log.warning("LUT missing at export: %s", lut_path)
            else:
                opacity = min(
                    1.0, max(0.0, self._as_float(extras.get("lut_opacity"), 1.0))
                )
                steps.append(
                    f"[{label}]split[luBase][luCopy];[luCopy]lut3d='{lut_path}'[luGraded];"
                    f"[luBase][luGraded]blend=all_mode=normal:all_opacity={opacity:.3f}[v1]"
                )
                label = "v1"

        extra_inputs: List[str] = []
        for key, default_opacity in (("dust_overlay", 0.35), ("scratch_overlay", 0.25)):
            overlay = extras.get(key)
            if overlay:
                if not Path(str(overlay)).exists():
                    self.log.warning("Overlay asset missing: %s", overlay)
                    continue
                index = len(extra_inputs) + 1
                opacity = min(
                    1.0,
                    max(
                        0.0,
                        self._as_float(extras.get(f"{key}_opacity"), default_opacity),
                    ),
                )
                extra_inputs.append(str(overlay))
                steps.append(
                    f"[{index}:v]scale={width}:{height},format=rgba,"
                    f"colorchannelmixer=aa={opacity:.2f}[ov{index}];"
                    f"[{label}][ov{index}]overlay=0:0:format=auto[v{index + 1}]"
                )
                label = f"v{index + 1}"
        return ";".join(steps), extra_inputs, label

    def _join_group(
        self,
        segments: List[Dict[str, Any]],
        transitions: List[Dict[str, Any]],
        preset: Dict[str, Any],
        output_path: Path,
        progress_callback: ProgressCallback,
        started: float,
    ) -> Dict[str, Any]:
        """Join a group of segments (recursively, in tens) with xfades."""
        if len(segments) == 1:
            command_in = [
                str(self._require_ffmpeg()),
                "-y",
                "-i",
                str(segments[0]["path"]),
            ]
            graph = "scale={}:{}".format(preset["width"], preset["height"])
            total = segments[0]["duration"]
            cmd = (
                command_in
                + ["-vf", graph]
                + self._codec_args(
                    self.get_encoder_for_preset(
                        preset, self.detect_hardware_acceleration()["data"]
                    )["data"]["codec"],
                    preset,
                )
                + [
                    "-pix_fmt",
                    preset["pixel_format"],
                    "-r",
                    str(preset["fps"]),
                    "-movflags",
                    preset["movflags"],
                    "-an",
                    str(output_path),
                ]
            )
            run = self._run_ffmpeg(
                cmd, int(total * preset["fps"]), progress_callback, started
            )
            if not run["success"]:
                return run
            return self.make_response(
                True, {"joined_segments": 1}, duration_ms=_ms(started)
            )

        if len(segments) <= XFADE_GROUP_SIZE:
            return self._xfade_join(
                segments, transitions, preset, output_path, progress_callback, started
            )

        chunks = [
            segments[i : i + XFADE_GROUP_SIZE]
            for i in range(0, len(segments), XFADE_GROUP_SIZE)
        ]
        self.log.info(
            "Joining %d segments in %d groups of %d",
            len(segments),
            len(chunks),
            XFADE_GROUP_SIZE,
        )
        transitions_per_group = max(0, XFADE_GROUP_SIZE - 1)
        pending: List[_GroupJob] = []
        group_outputs: List[Optional[Dict[str, Any]]] = [None] * len(chunks)
        for g, chunk in enumerate(chunks):
            if len(chunk) == 1:
                group_outputs[g] = chunk[0]
                continue
            chunk_transitions = transitions[
                g * transitions_per_group : (g + 1) * transitions_per_group
            ]
            pending.append(
                (g, chunk, chunk_transitions, output_path.with_suffix(f".group{g}.mp4"))
            )

        def _render_group(job: _GroupJob) -> _GroupResult:
            index, chunk, chunk_transitions, temp_out = job
            return index, chunk, chunk_transitions, temp_out, self._join_group(
                chunk, chunk_transitions, preset, temp_out, progress_callback, started
            )

        # PERFORMANCE (PHASE 8): each group is an independent ffmpeg
        # process writing to its own file, so groups render concurrently
        # instead of strictly one after another — this is where a
        # long, many-scene project spends most of its join time. The
        # resulting group files, their order, and the boundary join are
        # all exactly as before; only the scheduling changed. A single
        # pending group takes the plain sequential path (no pool).
        workers = max(1, min(MAX_PARALLEL_GROUP_JOINS, os.cpu_count() or 1))
        if len(pending) > 1 and workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                completed = list(pool.map(_render_group, pending))
        else:
            completed = [_render_group(job) for job in pending]

        for index, chunk, chunk_transitions, temp_out, joined in completed:
            if not joined["success"]:
                # PHASE 9: one failed group used to abandon every OTHER
                # group's intermediate file on disk — gigabytes on a long
                # project, left until the whole project was deleted.
                for _, _, _, other_temp, _ in completed:
                    safe_unlink(other_temp)
                return joined
            # BUGFIX (v3.1.1): the timeline never populates a "transitions"
            # list, so chunk_transitions was always [] and this bookkeeping
            # assumed zero crossfade shrinkage while _xfade_join (which
            # actually renders the group) applies a real 0.8s overlap per
            # boundary. That mismatch made the *recorded* duration longer
            # than the *actual* rendered file, which on multi-group renders
            # (>10 segments) drifted audio/subtitles out of sync with video
            # by ~0.8s per transition. Pad missing entries with the same
            # DEFAULT_TRANSITION duration _xfade_join falls back to, so the
            # bookkeeping always matches what ffmpeg actually produced.
            padded_transitions = [
                chunk_transitions[i] if i < len(chunk_transitions)
                else dict(DEFAULT_TRANSITION)
                for i in range(len(chunk) - 1)
            ]
            group_outputs[index] = {
                "path": temp_out,
                "duration": sum(c["duration"] for c in chunk)
                - self._xfade_loss(padded_transitions),
            }
        ordered = [group for group in group_outputs if group is not None]
        if len(ordered) == 1:
            if ordered[0]["path"] != output_path:
                # PHASE 8: move the finished group into place instead of
                # reading the whole file into memory and writing it back
                # out (os.replace is atomic and free on the same volume;
                # shutil.move still streams if it has to cross volumes).
                self._move_file(Path(ordered[0]["path"]), output_path)
            return self.make_response(
                True, {"joined_segments": len(segments)}, duration_ms=_ms(started)
            )
        # Join groups with default crossfade at boundaries (documented v1 rule).
        boundary = [dict(DEFAULT_TRANSITION) for _ in range(len(ordered) - 1)]
        result = self._xfade_join(
            ordered, boundary, preset, output_path, progress_callback, started
        )
        # PHASE 8: the intermediate group files are dead weight the moment
        # the boundary join has read them — on a long project that is
        # gigabytes of temp data left behind until project deletion.
        if result.get("success"):
            for _, _, _, temp_out, _ in completed:
                safe_unlink(temp_out)
        return result

    def _xfade_join(
        self,
        segments: List[Dict[str, Any]],
        transitions: List[Dict[str, Any]],
        preset: Dict[str, Any],
        output_path: Path,
        progress_callback: ProgressCallback,
        started: float,
    ) -> Dict[str, Any]:
        """Single-pass xfade join of <= XFADE_GROUP_SIZE segments."""
        codec = self.get_encoder_for_preset(
            preset, self.detect_hardware_acceleration()["data"]
        )["data"]["codec"]
        command = [str(self._require_ffmpeg()), "-y"]
        for seg in segments:
            command += ["-i", str(seg["path"])]
        running = float(segments[0]["duration"])
        parts: List[str] = []
        label = "0:v"
        for i in range(1, len(segments)):
            trans = (
                transitions[i - 1]
                if i - 1 < len(transitions)
                else dict(DEFAULT_TRANSITION)
            )
            ttype = str(trans.get("type") or "crossfade")
            tdur = min(
                float(trans.get("duration") or DEFAULT_TRANSITION["duration"]), running
            )
            offset = max(0.0, running - tdur)
            out = f"v{i:02d}"
            parts.append(
                f"[{label}][{i}:v]xfade=transition={self._xfade_name(ttype)}:"
                f"duration={tdur:.3f}:offset={offset:.3f}[{out}]"
            )
            label = out
            running = offset + float(segments[i]["duration"])
        parts.append(f"[{label}]scale={preset['width']}:{preset['height']}[vout]")
        total_frames = max(1, int(running * int(preset["fps"])))
        command += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
        command += self._codec_args(codec, preset)
        command += [
            "-pix_fmt",
            preset["pixel_format"],
            "-r",
            str(preset["fps"]),
            "-movflags",
            preset["movflags"],
            "-an",
            str(output_path),
        ]
        run = self._run_ffmpeg(command, total_frames, progress_callback, started)
        if not run["success"]:
            return run
        return self.make_response(
            True,
            {
                "joined_segments": len(segments),
                "total_duration": round(running, 3),
                "total_frames": total_frames,
            },
            duration_ms=_ms(started),
        )

    def _mux_audio(
        self,
        video_path: Path,
        audio_path: Path,
        preset: Dict[str, Any],
        progress_callback: ProgressCallback,
        started: float,
        audio_delay_ms: int = 0,
    ) -> Dict[str, Any]:
        """Mux the mixed audio track onto the joined video.

        BUGFIX (v3.1.1): previously relied on ffmpeg's ``-shortest`` flag to
        cut the output to the audio's length. On some ffmpeg/Windows builds
        ``-shortest`` does not reliably trim when combined with ``-c:v copy``,
        so the final file could end up with a video track far longer than its
        audio track (silent tail). We now explicitly probe the real audio
        duration and pass it as ``-t`` so the output length is deterministic
        and always matches the narration, on every platform/ffmpeg build.

        BUGFIX (v3.1.6): narration audio always started at t=0 of the final
        video, even when the video itself opens with a silent intro card —
        so the voice played early/during the intro, out of sync with the
        (correctly-timed, since v3.1.5) subtitles and scene images.
        audio_delay_ms shifts the narration to start at the same point the
        first real scene appears, via ffmpeg's adelay filter, keeping
        voice/captions/video all in sync.
        """
        temp = video_path.with_suffix(".mux.mp4")
        video_duration = self._probe_path_duration(video_path)
        # PHASE 8: the audio duration is only ever the fallback for an
        # unprobeable video (see the v3.1.3 note below), so the second
        # ffprobe process is only started when it can actually be used.
        audio_duration = (
            None if video_duration else self._probe_path_duration(audio_path)
        )
        # BUGFIX (v3.1.3): v3.1.1 targeted audio_duration here, which was
        # correct for the original silent-tail bug but wrong whenever video
        # is legitimately LONGER than the narration — e.g. a silent-by-design
        # outro/intro branding card that plays after narration ends. That
        # case is normal, not a bug, and forcing -t to the shorter audio
        # duration truncated real video content (confirmed: chopped a scene
        # off the end of a real render). The video track is already exactly
        # as long as it should be (built via -c:v copy from the join step),
        # so we now always target the video's own duration — audio simply
        # plays for its shorter length and ends naturally within it. We only
        # fall back to audio_duration if the video's duration can't be
        # probed at all.
        target_duration = video_duration or audio_duration
        command = [
            str(self._require_ffmpeg()),
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-c:v",
            "copy",
        ]
        if audio_delay_ms > 0:
            command += ["-af", f"adelay={audio_delay_ms}:all=1"]
        command += [
            "-c:a",
            str(preset["audio_codec"]),
            "-b:a",
            str(preset["audio_bitrate"]),
            "-ar",
            str(preset["audio_sample_rate"]),
            "-movflags",
            str(preset["movflags"]),
        ]
        if target_duration:
            command += ["-t", f"{target_duration:.3f}"]
        else:
            # Fallback only if ffprobe couldn't read either file's duration.
            command += ["-shortest"]
        command += [str(temp)]
        duration = target_duration or video_duration
        run = self._run_ffmpeg(
            command, int((duration or 60.0) * preset["fps"]), progress_callback, started
        )
        if not run["success"]:
            # PHASE 9: a failed mux leaves a partial .mux.mp4 beside the
            # real video — gigabytes of dead weight, and a confusing
            # extra file in the output folder. The joined video itself is
            # untouched, so the render can still be resumed/retried.
            safe_unlink(temp)
            return run
        # PHASE 9: the muxed file replaces the video only after ffmpeg
        # succeeded, via a retrying atomic rename (a Windows indexer or
        # media preview holding the destination briefly used to fail the
        # whole export at the very last step).
        try:
            replace_atomic(temp, video_path)
        except OSError as exc:
            self.log.error("Could not finalize muxed video %s: %s", video_path, exc)
            safe_unlink(temp)
            return self.make_response(
                False,
                error=f"could not write muxed video: {exc}",
                duration_ms=_ms(started),
            )
        return self.make_response(
            True,
            {"audio_path": str(audio_path), "audio_delay_ms": audio_delay_ms},
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def _run_ffmpeg(
        self,
        command: List[str],
        total_frames: int,
        callback: ProgressCallback,
        started: float,
    ) -> Dict[str, Any]:
        """Run FFmpeg with live progress parsing and a hard timeout (Rule 4).

        PHASE 9 (FFmpeg failure handling / process cleanup): the failure
        paths are hardened without touching the command or the encode.

        * The process is ALWAYS reaped. Previously a timeout killed it
          but never waited, leaving a zombie holding the output file
          open (on Windows that also blocks the following ``os.replace``
          of the muxed file); a ``KeyboardInterrupt``/cancel between
          spawn and wait leaked the process entirely.
        * ``process`` could be unbound in the handler when ``Popen``
          itself raised (a missing binary) — ``process.kill()`` then
          raised ``UnboundLocalError`` from inside an error path.
        * The tail of stderr is retained and reported, so a non-zero
          exit says WHY ffmpeg failed instead of only its exit code.
        """
        if not command or command[0] in (None, "None", ""):
            return self.make_response(
                False, error="FFmpeg not available", duration_ms=_ms(started)
            )
        preview = " ".join(str(part) for part in command)
        self.log.info(
            "FFmpeg command: %s",
            preview if len(preview) < 500 else preview[:500] + " ...[graph]",
        )
        process: Optional[subprocess.Popen] = None
        try:
            # PHASE 8 (rendering & export optimization): stdin is closed
            # explicitly (ffmpeg otherwise polls the inherited console
            # handle) and stderr is line-buffered so the progress reader
            # is woken per line instead of per default-sized block —
            # cheaper, and progress updates arrive promptly. No change to
            # the command itself, so the encode is byte-for-byte the same.
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            if callback:
                self.monitor_ffmpeg_progress(process, total_frames, callback)
                tail: List[str] = []
            else:
                tail = self._drain_stderr(process)
            result = process.wait(timeout=FFMPEG_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.error("FFmpeg run failed: %s", exc)
            self._terminate_process(process)
            return self.make_response(False, error=str(exc), duration_ms=_ms(started))
        except BaseException:
            # PHASE 9 (safe cancellation): Ctrl-C or a worker shutdown
            # must not leave an orphaned encoder writing to the output.
            self._terminate_process(process)
            raise
        if result != 0:
            detail = " | ".join(tail[-3:]).strip()
            error = f"ffmpeg exited with code {result}"
            if detail:
                error = f"{error}: {detail[:300]}"
                self.log.error("FFmpeg failed (rc=%s): %s", result, detail[:500])
            return self.make_response(False, error=error, duration_ms=_ms(started))
        return self.make_response(True, {"returncode": 0}, duration_ms=_ms(started))

    @staticmethod
    def _drain_stderr(process: subprocess.Popen, keep: int = 12) -> List[str]:
        """Consume ffmpeg's stderr, retaining the last few lines.

        Draining is REQUIRED (a full pipe deadlocks the encoder); PHASE 9
        keeps a bounded tail so a failure can be explained without
        holding an entire encode log in memory.
        """
        tail: List[str] = []
        for raw_line in process.stderr or []:
            line = str(raw_line).strip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > keep:
                del tail[0]
        return tail

    def _terminate_process(self, process: Optional[subprocess.Popen]) -> None:
        """Stop an ffmpeg process and reap it; never raises.

        PHASE 9 (process cleanup): terminate first so ffmpeg can close
        its output file, escalate to kill if it ignores that, and always
        ``wait()`` so no zombie survives holding the file open.
        """
        if process is None or process.poll() is not None:
            return
        for stopper in (process.terminate, process.kill):
            try:
                stopper()
                process.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue
            except (OSError, ValueError) as exc:
                self.log.warning("Could not stop FFmpeg process: %s", exc)
                return
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            self.log.warning("FFmpeg process did not exit after kill")

    def _discard_partial_output(self, output_path: str | Path) -> None:
        """Remove an output file left behind by a failed ffmpeg run.

        PHASE 9: ffmpeg creates its output file up front, so a failure
        (bad filter, disk full, cancellation) leaves a short or empty
        MP4. Anything already complete and playable is left alone — only
        files ffmpeg did not finish are removed, so a genuine retry
        starts clean while resumable work is preserved.
        """
        path = Path(str(output_path))
        try:
            if not path.is_file():
                return
            if path.stat().st_size == 0:
                safe_unlink(path)
                return
        except OSError:
            return
        if self._probe_path_duration(path) is None:
            self.log.warning("Removing unusable render output: %s", path)
            safe_unlink(path)

    @staticmethod
    def _move_file(source: Path, destination: Path) -> None:
        """Move a rendered file, preferring a rename over a byte copy.

        PHASE 8 (rendering & export optimization): a finished render can
        be several gigabytes; a same-volume rename costs nothing, while
        the previous read_bytes/write_bytes round trip cost a full read
        plus a full write plus holding the entire file in memory.

        PHASE 9: the rename now retries briefly before falling back to a
        cross-volume copy — on Windows a transient lock (indexer, media
        preview) on a freshly written MP4 is common and used to force a
        needless multi-gigabyte copy.
        """
        try:
            replace_atomic(source, destination)
        except OSError:
            shutil.move(str(source), str(destination))

    def _require_ffmpeg(self) -> Optional[str]:
        """Resolved ffmpeg path or None (callers guard)."""
        found = self.hardware.find_ffmpeg() if self.hardware else None
        return str(found) if found else None

    def _test_encoder(self, ffmpeg: str, encoder: str) -> bool:
        """Encode a tiny black clip; True when the encoder really works."""
        try:
            test = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=128x128:duration=0.2:rate=10",
                    "-c:v",
                    encoder,
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=ENCODER_PROBE_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return test.returncode == 0

    def _codec_args(self, codec: str, preset: Dict[str, Any]) -> List[str]:
        """Codec arguments: quality-based for every encoder, hardware or not.

        BUGFIX (v3.2.3): hardware encoders previously used a flat target
        bitrate (-b:v 8000k) applied uniformly to every scene, regardless
        of actual visual complexity — a slow-motion static outro card cost
        the same bits as a busy, detailed scene. Real test: a 20s
        low-motion card at flat 8000k came out 5x larger than the same
        clip at quality-based encoding, with no visible quality
        difference. Every encoder now uses its own quality-based mode
        (matching what CRF already did for software libx264), so simple
        content naturally uses fewer bits and detailed content still gets
        what it needs.
        """
        if codec in ("libx264", "libx265"):
            return [
                "-c:v", codec, "-preset", str(preset["preset"]),
                "-crf", str(preset["crf"]),
            ]
        if codec == "h264_qsv":
            # ICQ (Intelligent Constant Quality) — QSV's CRF equivalent.
            # Lower = higher quality; ~23 lines up with the CRF default
            # used elsewhere in this preset table.
            return [
                "-c:v", codec, "-global_quality",
                str(preset.get("crf", 23)), "-look_ahead", "1",
            ]
        if codec == "h264_nvenc":
            return [
                "-c:v", codec, "-rc", "vbr", "-cq",
                str(preset.get("crf", 23)),
            ]
        if codec == "h264_amf":
            return [
                "-c:v", codec, "-rc", "cqp", "-qp_i",
                str(preset.get("crf", 23)), "-qp_p", str(preset.get("crf", 23)),
            ]
        if codec == "h264_videotoolbox":
            return ["-c:v", codec, "-q:v", str(preset.get("crf", 23))]
        # Unknown hardware codec: fall back to the old flat-bitrate
        # behavior rather than guessing at unsupported flags.
        return ["-c:v", codec, "-b:v", str(preset["video_bitrate"])]

    def _normalize_segments(self, segment_list: List[Any]) -> List[Dict[str, Any]]:
        """Coerce segment entries to {'path': Path, 'duration': float}."""
        normalized: List[Dict[str, Any]] = []
        for i, seg in enumerate(segment_list):
            if isinstance(seg, dict):
                path = seg.get("path") or seg.get("segment_path")
                duration = seg.get("duration") or 8.0
            else:
                path, duration = seg, 8.0
            normalized.append({"path": Path(str(path)), "duration": float(duration)})
        return normalized

    def _timeline_duration(self, timeline: Dict[str, Any]) -> float:
        """Total seconds from a timeline dict (total_duration or scene sum)."""
        if not isinstance(timeline, dict):
            return 0.0
        if timeline.get("total_duration"):
            return float(timeline["total_duration"])
        scenes = timeline.get("scenes") or []
        if scenes:
            try:
                return sum(float(s.get("duration") or 0.0) for s in scenes)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def _publish_progress(self, data: Dict[str, Any]) -> None:
        """Best-effort render.progress event for future UI subscribers."""
        try:
            self.event_bus.publish("render.progress", data)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _subtitle_window(
        start_s: float, end_s: float, words: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """One subtitle segment window descriptor."""
        return {
            "start_s": round(start_s, 3),
            "end_s": round(max(end_s, start_s + 0.1), 3),
            "words": words,
            "word_count": len(words),
        }

    @staticmethod
    def _xfade_loss(transitions: List[Dict[str, Any]]) -> float:
        """Total seconds absorbed by transitions in a chunk."""
        return sum(float(t.get("duration") or 0.0) for t in transitions)

    @staticmethod
    def _xfade_name(autopilot_name: str) -> str:
        """Minimal mapping; transition_engine owns the full table (Rule 1)."""
        return {"crossfade": "fade", "hard_cut": "fade", "cut": "fade"}.get(
            autopilot_name, autopilot_name
        )

    @staticmethod
    def _wrap_title(text: str, max_chars: int) -> List[str]:
        """Wrap title card text into display lines."""
        words, lines, current = text.split(), [], ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) <= max_chars:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    @staticmethod
    def _probe_duration(data: Dict[str, Any]) -> Optional[float]:
        """Duration from ffprobe JSON (format first, then video stream)."""
        candidates: List[Any] = [(data.get("format") or {}).get("duration")]
        candidates += [
            s.get("duration")
            for s in (data.get("streams") or [])
            if s.get("codec_type") == "video"
        ]
        for value in candidates:
            if value is None:
                continue
            try:
                return float(str(value))
            except (TypeError, ValueError):
                continue
        return None

    def _probe_path_duration(self, path: Path) -> Optional[float]:
        """Best-effort duration probe for mux progress totals."""
        ffprobe = self.hardware.find_ffprobe() if self.hardware else None
        if not ffprobe:
            return None
        try:
            probe = subprocess.run(
                [
                    str(ffprobe),
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_format",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return self._probe_duration(json.loads(probe.stdout or "{}"))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _human_seconds(seconds: float) -> str:
        """Humanize a duration estimate."""
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60)}s"
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"

    def _load_config(self) -> None:
        """Load export_presets.json via ConfigService with file fallback."""
        try:
            data = self.config.get_config("export_presets")
        except Exception:  # noqa: BLE001
            data = {}
        if not data:
            path = Path("config/export_presets.json")
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    self.log.error(
                        "export_presets.json corrupt (%s); using built-ins", exc
                    )
                    return
        if not data:
            self.log.warning("export_presets.json missing; using built-in presets")
            return
        presets = {
            str(p.get("id")): {k: v for k, v in p.items() if k not in ("id", "name")}
            for p in (data.get("presets") or [])
            if p.get("id")
        }
        if presets:
            # BUGFIX (v3.2.6): this JSON file silently overrides the
            # in-code EXPORT_PRESETS defaults with NO log message when it
            # succeeds — only when it's missing. That silence let two
            # earlier fixes (128k audio bitrate in 3.2.3, CRF 21 in 3.2.6)
            # appear to "not work" on a real installation, because this
            # file still had the old values and nobody could tell it was
            # the active source. Always log which source is actually in
            # effect.
            self.log.info(
                "Export presets loaded from config/export_presets.json "
                "(overrides in-code defaults) — presets: %s",
                sorted(presets.keys()),
            )
            self._presets = presets
        if data.get("default_preset"):
            self._default = str(data["default_preset"])

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        """Best-effort float conversion with fallback."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)
