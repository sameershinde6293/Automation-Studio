"""Intro/outro engine: template-driven channel intro and outro segments.

Optional BaseModule (registry priority 12, CAN BE DISABLED: YES). Has no
File 07 module spec; built from the surrounding contract pieces:
modules_specification.txt timeline_engine ("Load intro/outro durations
from channel profile"), presets_and_configs.txt
config/default_channel_profile.json (intro/outro blocks), the
channel_profiles table (intro_*/outro_* columns), and
config/documentary_genres.json (per-genre intro_template/outro_template).

Renders 1920x1080 Pillow cards (gradient, ornament, text) and converts
them to MP4 segments with a local zoompan builder. RULE 1: no module is
imported from another — the tiny zoompan/segment logic is deliberately
local (documented), resolved via configs + DB only.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.safe_io import LazyAttribute
from core.service_container import BaseModule, ServiceContainer

Image = LazyAttribute("PIL", "Image")
ImageDraw = LazyAttribute("PIL", "ImageDraw")
ImageFont = LazyAttribute("PIL", "ImageFont")

MODULE_NAME = "intro_outro_engine"

WIDTH = 1920
HEIGHT = 1080
FPS = 30
MIN_DURATION = 0.5
MAX_DURATION = 60.0
DEFAULT_INTRO_DURATION = 5.0  # channel_profiles.intro_duration
DEFAULT_OUTRO_DURATION = 20.0  # channel_profiles.outro_duration
_DEFAULT_ZOOM_DELTA = 0.05
_FFMPEG_TIMEOUT = 300
_FFPROBE_TIMEOUT = 20
_SEGMENT_CRF = "21"  # matches the new app-wide default quality target (v3.2.6)
# PERFORMANCE (v3.2.2): was "slow" — intro/outro are branding cards with
# subtle motion only, so the quality difference vs "medium" at this CRF is
# negligible, but "medium" encodes meaningfully faster. Runs once per
# video (not per-scene), so the absolute time saved here is modest, but
# it's effectively free — no quality tradeoff worth mentioning at CRF 18.
_SEGMENT_PRESET = "medium"

# Ornament geometry (deterministic; covered by pixel tests)
_BAR_W = 480
_FRAME_INSET = 48
_FRAME_WIDTH = 4

# Canonical centered-crop zoompan axes (kept in sync with
# animation_engine's _CENTER_X/_CENTER_Y; duplicated per RULE 1).
_CENTER_X = "iw/2-(iw/zoom/2)"
_CENTER_Y = "ih/2-(ih/zoom/2)"


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def _hex_rgb(value: str, default: Tuple[int, int, int]) -> Tuple[int, int, int]:
    """Parse '#RRGGBB' into an (r, g, b) tuple."""
    text = str(value or "").strip().lstrip("#")
    if len(text) == 6:
        try:
            return (
                int(text[0:2], 16),
                int(text[2:4], 16),
                int(text[4:6], 16),
            )
        except ValueError:
            pass
    return default


def _fmt_delta(value: float) -> str:
    """Compact decimal for the zoom expression (0.06 -> '0.060')."""
    return f"{value:.3f}"


class IntroOutroEngine(BaseModule):
    """Generate template-driven intro/outro video segments."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize engine and load template/genre/profile configs."""
        super().__init__(container, MODULE_NAME)
        self._templates: Dict[str, Dict[str, Any]] = {}
        self._default_intro = "dark_history"
        self._default_outro = "dark_history"
        self._genre_templates: Dict[str, Dict[str, str]] = {}
        self._profile_config: Dict[str, Any] = {}
        self._load_configs()
        # Truetype search hints (tests may override to force fallback).
        self._font_search_paths: List[str] = [
            "assets/fonts",
            "fonts",
            "C:/Windows/Fonts",
            "/usr/share/fonts/truetype/dejavu",
            "/usr/share/fonts/truetype/msttcorefonts",
        ]

    def is_optional_module(self) -> bool:
        """Intro/outro is optional (registry required: false)."""
        return True

    # ------------------------------------------------------------------
    # Config loading (RULE 8)
    # ------------------------------------------------------------------
    def _load_configs(self) -> None:
        """Load intro_outro_templates, documentary_genres, channel profile."""
        try:
            data = self.config.get_config("intro_outro_templates") or {}
            for template in data.get("templates", []):
                if template.get("id"):
                    self._templates[template["id"]] = template
            self._default_intro = data.get("default_intro_template", "dark_history")
            self._default_outro = data.get("default_outro_template", "dark_history")
        except (OSError, ValueError, KeyError) as exc:
            self.log.warning("intro_outro_templates config unavailable: %s", exc)
        try:
            genres = self.config.get_config("documentary_genres") or {}
            for genre in genres.get("genres", []):
                gid = genre.get("id")
                if gid:
                    self._genre_templates[gid] = {
                        "intro_template": genre.get("intro_template") or "",
                        "outro_template": genre.get("outro_template") or "",
                    }
        except (OSError, ValueError, KeyError) as exc:
            self.log.warning("documentary_genres config unavailable: %s", exc)
        try:
            self._profile_config = (
                self.config.get_config("default_channel_profile") or {}
            )
        except (OSError, ValueError, KeyError) as exc:
            self.log.warning("default_channel_profile config unavailable: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_available_templates(self) -> Dict[str, Any]:
        """List the intro/outro template catalog."""
        started = time.perf_counter()
        templates = sorted(self._templates.values(), key=lambda t: t["id"])
        return self.make_response(
            True,
            {
                "templates": templates,
                "count": len(templates),
                "default_intro_template": self._default_intro,
                "default_outro_template": self._default_outro,
            },
            duration_ms=_ms(started),
        )

    def get_intro_outro_settings(self, project_id: str) -> Dict[str, Any]:
        """Resolve effective intro/outro settings for a project.

        Chain (later wins): default_channel_profile.json -> channel_profiles
        row (project's or default) -> documentary genre template fallback
        -> per-project has_intro/has_outro kill switches. Durations are
        clamped to [0.5, 60] seconds.
        """
        started = time.perf_counter()
        project = self.db.db.fetch_one(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        if project is None:
            return self.make_response(False, error=f"Project not found: {project_id}")

        base_intro = dict(self._profile_config.get("intro", {}) or {})
        base_outro = dict(self._profile_config.get("outro", {}) or {})
        profile = self._fetch_channel_profile(
            str(project.get("channel_profile_id") or "")
        )
        genre = str(project.get("genre") or "")

        intro = self._resolve_side(
            side="intro",
            base=base_intro,
            profile=profile,
            genre=genre,
            default_duration=DEFAULT_INTRO_DURATION,
        )
        outro = self._resolve_side(
            side="outro",
            base=base_outro,
            profile=profile,
            genre=genre,
            default_duration=DEFAULT_OUTRO_DURATION,
        )

        # Per-project kill switches (projects.has_intro/has_outro).
        if project.get("has_intro") is not None:
            intro["enabled"] = intro["enabled"] and bool(project.get("has_intro"))
        if project.get("has_outro") is not None:
            outro["enabled"] = outro["enabled"] and bool(project.get("has_outro"))

        channel_name = (
            (profile or {}).get("channel_name")
            or self._profile_config.get("channel_name")
            or "My Channel"
        )
        return self.make_response(
            True,
            {
                "project_id": project_id,
                "title": project.get("title") or "",
                "genre": genre,
                "channel_name": channel_name,
                "intro": intro,
                "outro": outro,
            },
            duration_ms=_ms(started),
        )

    def generate_intro(
        self,
        project_id: str,
        output_path: Optional[Any] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate the intro segment for a project (skipped when disabled)."""
        return self._generate_side("intro", project_id, output_path, overrides)

    def generate_outro(
        self,
        project_id: str,
        output_path: Optional[Any] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate the outro segment for a project (skipped when disabled)."""
        return self._generate_side("outro", project_id, output_path, overrides)

    def generate_template_preview(
        self,
        template_id: str,
        kind: str = "intro",
        output_path: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Render only the card PNG for a template (UI preview support)."""
        started = time.perf_counter()
        template = self._templates.get(template_id)
        if template is None:
            return self.make_response(
                False,
                error=f"Unknown intro/outro template: {template_id}",
                duration_ms=_ms(started),
            )
        card = self._render_card(
            kind,
            template,
            title="The Black Death",
            channel_name="My Channel",
        )
        dest = Path(output_path or f"preview_{kind}_{template_id}.png")
        dest.parent.mkdir(parents=True, exist_ok=True)
        card.save(dest, format="PNG")
        return self.make_response(
            True,
            {"preview_path": str(dest), "template": template_id, "kind": kind},
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Settings resolution
    # ------------------------------------------------------------------
    def _fetch_channel_profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        """Project's channel profile, falling back to the default one."""
        if profile_id:
            row = self.db.db.fetch_one(
                "SELECT * FROM channel_profiles WHERE id = ?", (profile_id,)
            )
            if row:
                return row
        return self.db.db.fetch_one(
            "SELECT * FROM channel_profiles WHERE is_default = 1 LIMIT 1"
        )

    def _resolve_side(
        self,
        side: str,
        base: Dict[str, Any],
        profile: Optional[Dict[str, Any]],
        genre: str,
        default_duration: float,
    ) -> Dict[str, Any]:
        """Merge config + profile + genre for one of intro/outro."""
        template_id = str(base.get("template") or "")
        duration = base.get("duration") or default_duration
        enabled = bool(base.get("enabled", True))
        custom_video = base.get("custom_video")

        if profile:
            pid = str(profile.get(f"{side}_template") or "")
            if pid:
                template_id = pid
            pdur = profile.get(f"{side}_duration")
            if pdur:
                duration = float(pdur)
            enabled = bool(profile.get(f"{side}_enabled"))
            pcustom = profile.get(f"{side}_custom_path")
            if pcustom:
                custom_video = pcustom

        if not template_id:
            template_id = self._genre_templates.get(genre, {}).get(f"{side}_template")
        if not template_id:
            template_id = (
                self._default_intro if side == "intro" else self._default_outro
            )

        clamped = min(MAX_DURATION, max(MIN_DURATION, float(duration)))
        return {
            "enabled": enabled,
            "template": template_id,
            "duration": clamped,
            "custom_video": custom_video,
        }

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------
    def _generate_side(
        self,
        kind: str,
        project_id: str,
        output_path: Optional[Any],
        overrides: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="intro_outro_engine is disabled")

        settings_result = self.get_intro_outro_settings(project_id)
        if not settings_result["success"]:
            return settings_result
        resolved = settings_result["data"]
        side = dict(resolved[kind])
        for key, value in (overrides or {}).items():
            if key in ("enabled", "template", "duration", "custom_video"):
                side[key] = value
        side["duration"] = min(MAX_DURATION, max(MIN_DURATION, float(side["duration"])))

        if not side["enabled"]:
            return self.make_response(
                True,
                {"kind": kind, "skipped": True, "reason": f"{kind} disabled"},
                duration_ms=_ms(started),
            )

        project = (
            self.db.db.fetch_one(
                "SELECT project_folder_path FROM projects WHERE id = ?", (project_id,)
            )
            or {}
        )
        base_folder = Path(
            output_path
            or (
                Path(str(project.get("project_folder_path") or "."))
                / "render"
                / f"{kind}.mp4"
            )
        )

        if side.get("custom_video"):
            return self._segment_from_custom_video(
                kind,
                str(side["custom_video"]),
                float(side["duration"]),
                base_folder,
                started,
            )

        template = self._templates.get(str(side["template"]))
        warnings: List[str] = []
        if template is None:
            warnings.append(f"Unknown template '{side['template']}', using 'default'")
            template = self._templates.get("default") or {}
        card = self._render_card(
            kind,
            template,
            title=str(resolved.get("title") or ""),
            channel_name=str(resolved.get("channel_name") or ""),
        )
        card_path = base_folder.with_suffix(f".{kind}_card.png")
        card_path.parent.mkdir(parents=True, exist_ok=True)
        card.save(card_path, format="PNG")

        segment = self._card_to_segment(
            card_path,
            float(side["duration"]),
            str(template.get("animation") or "static"),
            float(template.get("zoom_delta") or 0.0),
            base_folder,
        )
        if not segment["success"]:
            return segment
        segment["data"].update(
            {
                "kind": kind,
                "template": template.get("id"),
                "card_image": str(card_path),
                "duration": side["duration"],
                "skipped": False,
            }
        )
        segment["warnings"] = list(segment.get("warnings") or []) + warnings
        segment["duration_ms"] = _ms(started)
        return segment

    def _render_card(
        self,
        kind: str,
        template: Dict[str, Any],
        title: str,
        channel_name: str,
    ) -> Image.Image:
        """Render the 1920x1080 intro/outro card image."""
        top = _hex_rgb(template.get("background_top", "#101418"), (16, 20, 24))
        bottom = _hex_rgb(template.get("background_bottom", "#181C22"), (24, 28, 34))
        accent = _hex_rgb(template.get("accent_color", "#4A90D9"), (74, 144, 217))
        title_color = _hex_rgb(template.get("title_color", "#FFFFFF"), (255,) * 3)
        subtitle_color = _hex_rgb(template.get("subtitle_color", "#CCCCCC"), (204,) * 3)

        card = Image.new("RGB", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(card)
        for y in range(HEIGHT):  # vertical gradient
            ratio = y / max(1, HEIGHT - 1)
            draw.line(
                [(0, y), (WIDTH, y)],
                fill=tuple(int(a + (b - a) * ratio) for a, b in zip(top, bottom)),
            )

        title_font = self._load_font(
            str(template.get("font") or ""), int(template.get("title_font_size", 96))
        )
        subtitle_font = self._load_font(
            str(template.get("font") or ""),
            int(template.get("subtitle_font_size", 40)),
        )

        if kind == "intro":
            lines = self._wrap_text(draw, title.upper(), title_font, int(WIDTH * 0.8))
            block_h = len(lines) * 110
            self._draw_text_center(
                draw,
                str(channel_name).upper(),
                subtitle_font,
                HEIGHT // 2 - block_h // 2 - 70,
                subtitle_color,
            )
            y = HEIGHT // 2 - block_h // 2
            for line in lines:
                y = self._draw_text_center(draw, line, title_font, y, title_color, 110)
        else:
            block_top = HEIGHT // 2 - 150
            y = self._draw_text_center(
                draw, "THANKS FOR WATCHING", title_font, block_top, title_color, 110
            )
            y = self._draw_text_center(
                draw, str(channel_name), subtitle_font, y + 10, subtitle_color, 52
            )
            self._draw_text_center(
                draw, "Subscribe for more", subtitle_font, y + 4, accent, 52
            )

        self._draw_ornament(draw, str(template.get("ornament") or "accent_bar"), accent)
        return card

    def _draw_ornament(
        self, draw: ImageDraw.ImageDraw, style: str, accent: Tuple[int, int, int]
    ) -> None:
        """Template ornament at deterministic frame positions."""
        if style == "accent_bar":
            y0 = HEIGHT // 2 + 140
            draw.rectangle(
                [(WIDTH - _BAR_W) // 2, y0, (WIDTH + _BAR_W) // 2, y0 + 8],
                fill=accent,
            )
        elif style == "double_bar":
            y0 = HEIGHT // 2 + 130
            draw.rectangle(
                [(WIDTH - _BAR_W) // 2, y0, (WIDTH + _BAR_W) // 2, y0 + 2],
                fill=accent,
            )
            draw.rectangle(
                [(WIDTH - _BAR_W) // 2, y0 + 22, (WIDTH + _BAR_W) // 2, y0 + 32],
                fill=accent,
            )
        elif style == "frame_lines":
            for offset in range(_FRAME_WIDTH):
                draw.rectangle(
                    [
                        (_FRAME_INSET + offset, _FRAME_INSET + offset),
                        (
                            WIDTH - _FRAME_INSET - 1 - offset,
                            HEIGHT - _FRAME_INSET - 1 - offset,
                        ),
                    ],
                    outline=accent,
                )

    def _load_font(self, preferred_name: str, size: int) -> ImageFont.ImageFont:
        """Best-effort truetype load with graceful degradation."""
        candidates = []
        for folder in self._font_search_paths:
            if preferred_name:
                candidates.append(Path(folder) / f"{preferred_name}.ttf")
        candidates.extend(
            [
                Path(f) / name
                for f in self._font_search_paths
                for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf")
            ]
        )
        for candidate in candidates:
            try:
                if candidate.exists():
                    return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _wrap_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.ImageFont,
        max_px: int,
    ) -> List[str]:
        """Word-wrap text to a pixel width (max 3 lines, ellipsized)."""
        words = str(text or "").split()
        if not words:
            return [""]
        lines: List[str] = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) <= max_px or not current:
                current = trial
            else:
                lines.append(current)
                current = word
            if len(lines) == 2:  # third line is the last one
                break
        remaining = words[len(" ".join([*lines, current]).split()) :]
        if remaining:
            current = f"{current} {remaining[0]}…" if current else f"{remaining[0]}…"
        lines.append(current)
        return lines[:3]

    @staticmethod
    def _draw_text_center(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.ImageFont,
        y: int,
        fill: Tuple[int, int, int],
        line_step: int = 0,
    ) -> int:
        """Draw one centered line; return the next baseline y."""
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((WIDTH - (box[2] - box[0])) // 2, y), text, font=font, fill=fill)
        return y + (line_step or (box[3] - box[1]) + 12)

    # ------------------------------------------------------------------
    # Segment rendering (local FFmpeg usage; RULE 4 logs the command)
    # ------------------------------------------------------------------
    def _build_zoompan(
        self, animation: str, zoom_delta: float, total_frames: int
    ) -> str:
        """Local zoompan expression for card segments (RULE 1: no imports)."""
        if animation == "slow_zoom_out":
            zoom = f"{_fmt_delta(1.0 + zoom_delta)}-{_fmt_delta(zoom_delta)}*on/{total_frames}"
        elif animation == "slow_zoom_in":
            zoom = f"1+{_fmt_delta(zoom_delta)}*on/{total_frames}"
        else:
            zoom = "1"
        return (
            f"zoompan=z='{zoom}':x='{_CENTER_X}':y='{_CENTER_Y}'"
            f":d={total_frames}:s={WIDTH}x{HEIGHT}:fps={FPS}"
        )

    def _card_to_segment(
        self,
        card_path: Path,
        duration: float,
        animation: str,
        zoom_delta: float,
        output_path: Path,
    ) -> Dict[str, Any]:
        """Convert a card PNG into an MP4 segment via ffmpeg."""
        started = time.perf_counter()
        ffmpeg = self.hardware.find_ffmpeg()
        if ffmpeg is None:
            return self.make_response(
                False, error="FFmpeg not available", duration_ms=_ms(started)
            )
        total_frames = max(1, int(duration * FPS))
        zoompan = self._build_zoompan(animation, zoom_delta, total_frames)
        command = [
            str(ffmpeg),
            "-y",
            "-loop",
            "1",
            "-i",
            str(card_path),
            "-t",
            f"{duration:.3f}",
            "-vf",
            zoompan,
            "-c:v",
            "libx264",
            "-preset",
            _SEGMENT_PRESET,
            "-crf",
            _SEGMENT_CRF,
            "-r",
            str(FPS),
            "-s",
            f"{WIDTH}x{HEIGHT}",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(output_path),
        ]
        return self._run_segment_command(command, output_path, started)

    def _segment_from_custom_video(
        self,
        kind: str,
        custom_path: str,
        duration: float,
        output_path: Path,
        started: float,
    ) -> Dict[str, Any]:
        """Trim/normalize a user-provided intro/outro video file."""
        source = Path(custom_path)
        if not source.exists():
            return self.make_response(
                False,
                error=f"Custom {kind} video not found: {custom_path}",
                duration_ms=_ms(started),
            )
        ffmpeg = self.hardware.find_ffmpeg()
        if ffmpeg is None:
            return self.make_response(
                False, error="FFmpeg not available", duration_ms=_ms(started)
            )
        probed = self._probe_duration(source)
        trim = duration if probed <= 0 else min(duration, probed)
        scale_pad = (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
        )
        command = [
            str(ffmpeg),
            "-y",
            "-i",
            str(source),
            "-t",
            f"{trim:.3f}",
            "-vf",
            scale_pad,
            "-c:v",
            "libx264",
            "-preset",
            _SEGMENT_PRESET,
            "-crf",
            _SEGMENT_CRF,
            "-r",
            str(FPS),
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(output_path),
        ]
        result = self._run_segment_command(command, output_path, started)
        if result["success"]:
            result["data"].update(
                {
                    "kind": kind,
                    "template": None,
                    "custom_video": str(source),
                    "duration": trim,
                    "skipped": False,
                }
            )
        return result

    def _run_segment_command(
        self, command: List[str], output_path: Path, started: float
    ) -> Dict[str, Any]:
        """Run an ffmpeg segment command and verify the output (RULE 7/4)."""
        self.log.info("FFmpeg command: %s", command)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self.make_response(
                False, error=f"FFmpeg execution failed: {exc}", duration_ms=_ms(started)
            )
        if result.returncode != 0:
            tail = (result.stderr or "")[-300:]
            return self.make_response(
                False,
                error=f"FFmpeg exited with code {result.returncode}: {tail}",
                duration_ms=_ms(started),
            )
        if not output_path.exists() or output_path.stat().st_size == 0:
            return self.make_response(
                False,
                error="FFmpeg produced no output file",
                duration_ms=_ms(started),
            )
        return self.make_response(
            True,
            {"segment_path": str(output_path), "command_logged": True},
            duration_ms=_ms(started),
        )

    def _probe_duration(self, video_path: Path) -> float:
        """ffprobe duration probe; 0.0 when unavailable/failed."""
        ffprobe = self.hardware.find_ffprobe()
        if ffprobe is None:
            return 0.0
        command = [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(video_path),
        ]
        self.log.info("FFprobe command: %s", command)
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=_FFPROBE_TIMEOUT
            )
            data = json.loads(result.stdout or "{}")
            return float(data.get("format", {}).get("duration") or 0.0)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            self.log.warning("ffprobe failed for %s: %s", video_path, exc)
            return 0.0
