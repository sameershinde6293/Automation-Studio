"""Thumbnail generator: Pillow-based YouTube thumbnail variations.

Optional BaseModule (registry priority 14, CAN BE DISABLED: YES). Has no
File 07 module spec; built from the surrounding contract pieces:
the ``thumbnails`` table (schema.sql: variation_number 1-5, 1280x720,
is_selected), app_settings (``auto_generate_thumbnails``,
``thumbnail_count`` = 5), ``channel_profiles.thumbnail_style`` +
``config/default_channel_profile.json``, ``config/thumbnail_styles.json``
(new v1 config), and File 15 ``test_thumbnail_files_generated``.

Renders 1280x720 JPEG variations: a scene image (or styled gradient
card when no scene image exists), dark overlay + vignette, accent
ornament, wrapped title text, channel text. No FFmpeg is involved -
thumbnails are pure Pillow composites. RULE 1: no module is imported
from another; the tiny gradient/wrap/font/ornament helpers are
deliberately local (documented duplicates of the C.4 card helpers).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.safe_io import LazyAttribute
from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

Image = LazyAttribute("PIL", "Image")
ImageDraw = LazyAttribute("PIL", "ImageDraw")
ImageFont = LazyAttribute("PIL", "ImageFont")
ImageOps = LazyAttribute("PIL", "ImageOps")

MODULE_NAME = "thumbnail_generator"

WIDTH = 1280
HEIGHT = 720
MIN_VARIATIONS = 1
MAX_VARIATIONS = 5  # schema: variation_number "1 through 5"
DEFAULT_STYLE = "dark_history"
JPEG_QUALITY = 92

# Layout (deterministic; covered by pixel tests). Ornament geometry
# mirrors intro_outro_engine so branding matches across assets.
_BAR_W = 480
_FRAME_INSET = 48
_FRAME_WIDTH = 4
_TITLE_BOTTOM = HEIGHT - 140  # title block bottom edge
_ORNAMENT_Y = HEIGHT - 118  # accent bar just under the title
_CHANNEL_Y = HEIGHT - 96  # bottom-centered channel text
_VIGNETTE_STRENGTH = 0.5


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def _hex_rgb(value: str, default: Tuple[int, int, int]) -> Tuple[int, int, int]:
    """Parse '#RRGGBB' into an (r, g, b) tuple.

    Duplicate of the intro_outro_engine helper (RULE 1: no imports).
    """
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


class ThumbnailGenerator(BaseModule):
    """Generate 1280x720 YouTube thumbnail variations for a project."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize module and load style/profile configs (RULE 8)."""
        super().__init__(container, MODULE_NAME)
        self._styles: Dict[str, Dict[str, Any]] = {}
        self._style_order: List[str] = []
        self._default_style = DEFAULT_STYLE
        self._default_count = MAX_VARIATIONS
        self._jpeg_quality = JPEG_QUALITY
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
        """Thumbnail generation is optional (registry required: false)."""
        return True

    # ------------------------------------------------------------------
    # Config loading (RULE 8: validated, failure-tolerant)
    # ------------------------------------------------------------------
    def _load_configs(self) -> None:
        """Load thumbnail_styles.json and default_channel_profile.json."""
        try:
            data = self.config.get_config("thumbnail_styles") or {}
            for style in data.get("styles", []):
                sid = style.get("id")
                if sid:
                    self._styles[sid] = style
                    self._style_order.append(sid)
            self._default_style = str(
                data.get("default_style") or DEFAULT_STYLE
            )
            self._default_count = int(data.get("default_count") or MAX_VARIATIONS)
            self._jpeg_quality = int(data.get("jpeg_quality") or JPEG_QUALITY)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.log.warning("thumbnail_styles config unavailable: %s", exc)
        try:
            self._profile_config = (
                self.config.get_config("default_channel_profile") or {}
            )
        except (OSError, ValueError, KeyError) as exc:
            self.log.warning("default_channel_profile config unavailable: %s", exc)
        if self._default_style not in self._styles:
            self._default_style = (
                self._style_order[0] if self._style_order else DEFAULT_STYLE
            )

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    def _fetch_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        return self.db.db.fetch_one(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )

    def _fetch_channel_profile(self, ref: str) -> Optional[Dict[str, Any]]:
        """Channel profile by id, then profile_name, then is_default row.

        projects.channel_profile_id stores either the row id
        (``profile_default``) or the profile name (``default``) -
        schema uses both; resolve defensively like C.4 (documented).
        """
        row = self.db.db.fetch_one(
            "SELECT * FROM channel_profiles WHERE id = ?", (ref,)
        )
        if row is None:
            row = self.db.db.fetch_one(
                "SELECT * FROM channel_profiles WHERE profile_name = ?",
                (ref,),
            )
        if row is None:
            row = self.db.db.fetch_one(
                "SELECT * FROM channel_profiles WHERE is_default = 1"
                " ORDER BY created_at LIMIT 1"
            )
        return row

    def _scene_sources(self, project_id: str) -> List[Tuple[str, float]]:
        """Existing scene images with their timeline start timestamps.

        Ordered by scene_number; timestamp = cumulative duration of the
        preceding scenes (used for thumbnails.source_timestamp).
        """
        scenes = self.db.db.fetch_all(
            "SELECT image_file_path, duration FROM scenes"
            " WHERE project_id = ? ORDER BY scene_number",
            (project_id,),
        )
        sources: List[Tuple[str, float]] = []
        started_at = 0.0
        for scene in scenes:
            raw = scene.get("image_file_path")
            if raw and Path(str(raw)).exists():
                sources.append((str(raw), round(started_at, 3)))
            try:
                started_at += max(0.0, float(scene.get("duration") or 0.0))
            except (TypeError, ValueError):
                continue
        return sources

    def _resolve_style(self, style_id: str) -> Dict[str, Any]:
        """Style dict by id; falls back to the configured default."""
        style = self._styles.get(style_id) or {}
        if not style:
            style = self._styles.get(self._default_style) or {}
        return {
            "id": style.get("id") or self._default_style,
            "background_top": style.get("background_top", "#23262B"),
            "background_bottom": style.get("background_bottom", "#101216"),
            "overlay_opacity": float(style.get("overlay_opacity", 0.55)),
            "vignette": bool(style.get("vignette", True)),
            "accent_color": style.get("accent_color", "#B0B6BF"),
            "ornament": style.get("ornament", "accent_bar"),
            "title_color": style.get("title_color", "#FFFFFF"),
            "title_size": int(style.get("title_size", 80)),
            "channel_color": style.get("channel_color", "#C9CDD3"),
            "channel_size": int(style.get("channel_size", 34)),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_available_styles(self) -> Dict[str, Any]:
        """List the thumbnail style catalog."""
        started = time.perf_counter()
        styles = [self._resolve_style(sid) for sid in sorted(self._styles)]
        return self.make_response(
            True,
            {
                "styles": styles,
                "count": len(styles),
                "default_style": self._default_style,
                "max_variations": MAX_VARIATIONS,
            },
            duration_ms=_ms(started),
        )

    def get_thumbnail_settings(self, project_id: str) -> Dict[str, Any]:
        """Resolve effective thumbnail settings for a project.

        Chain (later wins): thumbnail_styles default -> channel_profiles
        row (project's or default) -> default_channel_profile.json ->
        app_settings thumbnail_count/auto_generate_thumbnails. Count is
        clamped to [1, 5] (thumbnails table contract).
        """
        started = time.perf_counter()
        project = self._fetch_project(project_id)
        if project is None:
            return self.make_response(
                False, error=f"Project not found: {project_id}"
            )

        profile = self._fetch_channel_profile(
            str(project.get("channel_profile_id") or "")
        )
        profile = profile or {}
        thumb_block = self._profile_config.get("thumbnail", {}) or {}

        style_id = (
            profile.get("thumbnail_style")
            or thumb_block.get("style")
            or self._default_style
        )
        channel_text = (
            profile.get("channel_name")
            or self._profile_config.get("channel_name")
            or "My Channel"
        )
        title_text = str(project.get("title") or "")

        try:
            count = int(self.config.get("thumbnail_count", self._default_count))
        except (TypeError, ValueError):
            count = self._default_count
        count = min(MAX_VARIATIONS, max(MIN_VARIATIONS, count))
        auto_enabled = bool(self.config.get("auto_generate_thumbnails", True))

        folder = Path(str(project.get("project_folder_path") or "."))
        return self.make_response(
            True,
            {
                "project_id": project_id,
                "title_text": title_text,
                "channel_text": str(channel_text),
                "style_id": str(style_id),
                "count": count,
                "auto_enabled": auto_enabled,
                "profile_id": profile.get("id"),
                "output_folder": str(folder / "thumbnails"),
            },
            duration_ms=_ms(started),
        )

    def generate_thumbnails(
        self,
        project_id: str,
        count: Optional[int] = None,
        style_ids: Optional[List[str]] = None,
        title_text: Optional[str] = None,
        channel_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate up to 5 thumbnail variations for a project.

        Each variation cycles a style (profile style first, then the
        remaining catalog styles) and a source scene image (falling back
        to a styled gradient card). Rows are persisted to the
        ``thumbnails`` table and a ``thumbnails.generated`` event is
        published. Per-variation failures degrade to warnings (RULE 7).
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(
                False, error="thumbnail_generator is disabled"
            )

        settings = self.get_thumbnail_settings(project_id)
        if not settings["success"]:
            return settings
        resolved = settings["data"]
        warnings: List[str] = []

        total = count if count is not None else int(resolved["count"])
        total = min(MAX_VARIATIONS, max(MIN_VARIATIONS, int(total)))
        title = str(title_text if title_text is not None else resolved["title_text"])
        channel = str(
            channel_text if channel_text is not None else resolved["channel_text"]
        )

        # Style cycle: explicit list, else profile style then catalog.
        cycle: List[str] = []
        if style_ids:
            for sid in style_ids:
                if sid in self._styles:
                    cycle.append(sid)
                else:
                    warnings.append(f"unknown thumbnail style ignored: {sid}")
        profile_style = str(resolved["style_id"])
        if profile_style in self._styles and profile_style not in cycle:
            cycle.append(profile_style)
        for sid in self._style_order:
            if sid not in cycle:
                cycle.append(sid)
        if not cycle:
            cycle = [self._default_style]
            warnings.append(
                "thumbnail style catalog empty; using built-in defaults"
            )

        sources = self._scene_sources(project_id)
        folder = Path(str(resolved["output_folder"]))
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self.make_response(
                False, error=f"cannot create thumbnail folder: {exc}"
            )

        rows: List[Dict[str, Any]] = []
        now = utc_now_str()
        for index in range(1, total + 1):
            style = self._resolve_style(cycle[(index - 1) % len(cycle)])
            source_path: Optional[str] = None
            timestamp = 0.0
            if sources:
                source_path, timestamp = sources[(index - 1) % len(sources)]
            try:
                image = self._compose(source_path, style, title, channel)
            except Exception as exc:  # corrupt source -> gradient fallback
                self.log.warning("thumbnail compose failed (%s); fallback", exc)
                warnings.append(f"variation {index}: source unusable ({exc})")
                try:
                    image = self._compose(None, style, title, channel)
                    source_path = None
                    timestamp = 0.0
                except Exception as fatal:
                    warnings.append(f"variation {index}: failed ({fatal})")
                    continue

            file_path = folder / f"thumb_{index:02d}_{style['id']}.jpg"
            try:
                image.save(file_path, format="JPEG", quality=self._jpeg_quality)
            except OSError as exc:
                warnings.append(f"variation {index}: save failed ({exc})")
                continue

            row_id = self.db.new_id()
            self.db.db.execute(
                "INSERT INTO thumbnails"
                " (id, project_id, render_session_id, variation_number,"
                " file_path, source_timestamp, style_applied, title_text,"
                " channel_text, file_size_bytes, width, height, is_selected,"
                " created_at)"
                " VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    row_id,
                    project_id,
                    index,
                    str(file_path),
                    timestamp,
                    style["id"],
                    title,
                    channel,
                    file_path.stat().st_size,
                    WIDTH,
                    HEIGHT,
                    now,
                ),
            )
            rows.append(
                {
                    "id": row_id,
                    "variation_number": index,
                    "file_path": str(file_path),
                    "source_timestamp": timestamp,
                    "style_applied": style["id"],
                    "title_text": title,
                    "channel_text": channel,
                    "file_size_bytes": file_path.stat().st_size,
                    "width": WIDTH,
                    "height": HEIGHT,
                    "is_selected": False,
                }
            )

        if not rows:
            return self.make_response(
                False,
                error="no thumbnails could be generated",
                warnings=warnings,
                duration_ms=_ms(started),
            )

        self.event_bus.publish(
            "thumbnails.generated",
            {
                "project_id": project_id,
                "count": len(rows),
                "file_paths": [r["file_path"] for r in rows],
            },
        )
        return self.make_response(
            True,
            {
                "project_id": project_id,
                "count": len(rows),
                "thumbnails": rows,
                "folder": str(folder),
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def auto_generate_for_project(self, project_id: str) -> Dict[str, Any]:
        """Pipeline seam: generate only when auto_generate_thumbnails=1."""
        started = time.perf_counter()
        if not bool(self.config.get("auto_generate_thumbnails", True)):
            return self.make_response(
                True,
                {
                    "project_id": project_id,
                    "skipped": True,
                    "reason": "auto_generate_thumbnails disabled",
                },
                duration_ms=_ms(started),
            )
        return self.generate_thumbnails(project_id)

    def list_thumbnails(self, project_id: str) -> Dict[str, Any]:
        """List persisted thumbnails for a project (variation order)."""
        started = time.perf_counter()
        rows = self.db.db.fetch_all(
            "SELECT * FROM thumbnails WHERE project_id = ?"
            " ORDER BY variation_number",
            (project_id,),
        )
        return self.make_response(
            True,
            {"project_id": project_id, "count": len(rows), "thumbnails": rows},
            duration_ms=_ms(started),
        )

    def select_thumbnail(
        self, project_id: str, thumbnail_id: str
    ) -> Dict[str, Any]:
        """Mark one thumbnail as the selected one (exclusive per project)."""
        started = time.perf_counter()
        row = self.db.db.fetch_one(
            "SELECT id FROM thumbnails WHERE id = ? AND project_id = ?",
            (thumbnail_id, project_id),
        )
        if row is None:
            return self.make_response(
                False, error=f"Thumbnail not found: {thumbnail_id}"
            )
        self.db.db.execute(
            "UPDATE thumbnails SET is_selected = 0 WHERE project_id = ?",
            (project_id,),
        )
        self.db.db.execute(
            "UPDATE thumbnails SET is_selected = 1 WHERE id = ?",
            (thumbnail_id,),
        )
        return self.make_response(
            True,
            {"project_id": project_id, "selected_id": thumbnail_id},
            duration_ms=_ms(started),
        )

    def delete_thumbnail(self, thumbnail_id: str) -> Dict[str, Any]:
        """Delete one thumbnail row; the file is removed best-effort."""
        started = time.perf_counter()
        row = self.db.db.fetch_one(
            "SELECT id, file_path FROM thumbnails WHERE id = ?",
            (thumbnail_id,),
        )
        if row is None:
            return self.make_response(
                False, error=f"Thumbnail not found: {thumbnail_id}"
            )
        warnings: List[str] = []
        try:
            Path(str(row.get("file_path") or "")).unlink(missing_ok=True)
        except OSError as exc:
            warnings.append(f"file removal failed: {exc}")
        self.db.db.execute(
            "DELETE FROM thumbnails WHERE id = ?", (thumbnail_id,)
        )
        return self.make_response(
            True, {"deleted_id": thumbnail_id}, warnings=warnings,
            duration_ms=_ms(started),
        )

    def delete_project_thumbnails(self, project_id: str) -> Dict[str, Any]:
        """Delete all thumbnails of a project (rows + files best-effort)."""
        started = time.perf_counter()
        rows = self.db.db.fetch_all(
            "SELECT id, file_path FROM thumbnails WHERE project_id = ?",
            (project_id,),
        )
        removed = 0
        for row in rows:
            try:
                Path(str(row.get("file_path") or "")).unlink(missing_ok=True)
            except OSError:
                continue
            removed += 1
        self.db.db.execute(
            "DELETE FROM thumbnails WHERE project_id = ?", (project_id,)
        )
        return self.make_response(
            True,
            {"project_id": project_id, "deleted": len(rows), "files_removed": removed},
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Composition (pure Pillow; deterministic geometry)
    # ------------------------------------------------------------------
    def _compose(
        self,
        source_path: Optional[str],
        style: Dict[str, Any],
        title_text: str,
        channel_text: str,
    ) -> Image.Image:
        """Compose one RGB thumbnail image (source image or gradient)."""
        if source_path:
            with Image.open(source_path) as src:
                base = ImageOps.fit(
                    src.convert("RGB"), (WIDTH, HEIGHT), Image.LANCZOS
                )
        else:
            base = self._gradient_card(style)

        opacity = min(1.0, max(0.0, float(style["overlay_opacity"])))
        if opacity > 0.0:
            black = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
            base = Image.blend(base, black, opacity)
        if style["vignette"]:
            base = self._apply_vignette(base)

        draw = ImageDraw.Draw(base)
        accent = _hex_rgb(str(style["accent_color"]), (176, 182, 191))
        self._draw_ornament(draw, str(style["ornament"]), accent)

        title_font = self._load_font("Montserrat-Bold", int(style["title_size"]))
        channel_font = self._load_font(
            "Montserrat-Bold", int(style["channel_size"])
        )
        title_color = _hex_rgb(str(style["title_color"]), (255,) * 3)
        channel_color = _hex_rgb(str(style["channel_color"]), (204,) * 3)

        lines = self._wrap_text(
            draw, title_text.upper(), title_font, int(WIDTH * 0.86)
        )
        line_step = int(int(style["title_size"]) * 1.15) + 12
        block_h = len(lines) * line_step
        y = _TITLE_BOTTOM - block_h
        for line in lines:
            y = self._draw_text_center(
                draw, line, title_font, y, title_color, line_step
            )
        if channel_text:
            self._draw_text_center(
                draw,
                str(channel_text),
                channel_font,
                _CHANNEL_Y,
                channel_color,
            )
        return base

    def _gradient_card(self, style: Dict[str, Any]) -> Image.Image:
        """Styled gradient fallback card (no scene image available).

        Same vertical-gradient approach as the C.4 intro/outro cards
        (documented RULE 1 duplication).
        """
        top = _hex_rgb(str(style["background_top"]), (35, 38, 43))
        bottom = _hex_rgb(str(style["background_bottom"]), (16, 18, 22))
        card = Image.new("RGB", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(card)
        for y in range(HEIGHT):
            ratio = y / max(1, HEIGHT - 1)
            draw.line(
                [(0, y), (WIDTH, y)],
                fill=tuple(int(a + (b - a) * ratio) for a, b in zip(top, bottom)),
            )
        return card

    @staticmethod
    def _apply_vignette(image: Image.Image) -> Image.Image:
        """Darken edges with a radial mask (corners darker than center)."""
        mask = Image.radial_gradient("L").resize((WIDTH, HEIGHT))
        alpha = mask.point(lambda v: int(v * _VIGNETTE_STRENGTH))
        shade = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        image.paste(shade, (0, 0), alpha)
        return image

    def _draw_ornament(
        self, draw: ImageDraw.ImageDraw, style: str, accent: Tuple[int, int, int]
    ) -> None:
        """Accent ornament at deterministic positions (mirrors C.4)."""
        if style == "accent_bar":
            draw.rectangle(
                [
                    ((WIDTH - _BAR_W) // 2, _ORNAMENT_Y),
                    ((WIDTH + _BAR_W) // 2, _ORNAMENT_Y + 8),
                ],
                fill=accent,
            )
        elif style == "double_bar":
            draw.rectangle(
                [
                    ((WIDTH - _BAR_W) // 2, _ORNAMENT_Y),
                    ((WIDTH + _BAR_W) // 2, _ORNAMENT_Y + 2),
                ],
                fill=accent,
            )
            draw.rectangle(
                [
                    ((WIDTH - _BAR_W) // 2, _ORNAMENT_Y + 22),
                    ((WIDTH + _BAR_W) // 2, _ORNAMENT_Y + 32),
                ],
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
        """Best-effort truetype load with graceful degradation.

        Duplicate of the intro_outro_engine helper (RULE 1: no imports).
        """
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
        """Word-wrap text to a pixel width (max 3 lines, ellipsized).

        Duplicate of the intro_outro_engine helper (RULE 1: no imports).
        """
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
