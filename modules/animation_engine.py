"""Animation engine: FFmpeg zoompan filter generation for still-image scenes.

Optional BaseModule (can be disabled safely). Converts still images into
animated segments (Ken Burns, zooms, pans, static hold) by generating
``zoompan`` filter strings for the later export_engine (B.12).

Pure string generation: no FFmpeg, OpenCV, or Pillow required for v1.
Spec sources: modules_specification.txt MODULE 10 (presets/algorithm) and
presets_and_configs.txt config/animation_presets.json (13-animation catalog).
"""

from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.service_container import BaseModule, ServiceContainer

MODULE_NAME = "animation_engine"

# Canonical FFmpeg expressions for a centered crop window (per-axis).
_CENTER_X = "iw/2-(iw/zoom/2)"
_CENTER_Y = "ih/2-(ih/zoom/2)"

# Clamp ranges
MIN_ZOOM = 1.0
MAX_ZOOM = 3.0
MIN_DURATION = 0.1
MAX_DURATION = 600.0
MIN_FPS = 1
MAX_FPS = 120
DEFAULT_FPS = 30
DEFAULT_WIDTH = 1920
# FEATURE (v3.2.4): scenes held longer than this get oscillating
# (breathing) motion instead of one slow single-direction arc — see
# _oscillating_progress_expr. One cycle roughly every 35s felt natural in
# testing: fast enough to read as clearly alive, slow enough to stay calm
# and not distracting for a documentary-style hold.
LONG_HOLD_THRESHOLD_S = 40.0
LONG_HOLD_CYCLE_SECONDS = 35.0
DEFAULT_HEIGHT = 1080

# Built-in fallback weights (File 07 DOCUMENTARY_ANIMATION_WEIGHTS). Runtime
# values normally come from config/animation_presets.json.
BUILTIN_DOCUMENTARY_WEIGHTS: Dict[str, int] = {
    "slow_zoom_in": 30,
    "slow_zoom_out": 20,
    "ken_burns": 25,
    "pan_left": 10,
    "pan_right": 10,
    "static": 5,
}

BUILTIN_INTENSITY_MULTIPLIERS: Dict[str, float] = {
    "subtle": 0.50,
    "medium": 1.00,
    "dramatic": 1.50,
}

# Built-in fallback mood map (union of File 07/File 11; File 11 wins where
# they differ, e.g. document -> vertical_scan).
BUILTIN_MOOD_MAP: Dict[str, str] = {
    "dramatic": "dramatic_zoom_in",
    "mysterious": "slow_zoom_in",
    "ominous": "slow_zoom_in",
    "calm": "slow_zoom_out",
    "solemn": "slow_zoom_out",
    "memorial": "slow_zoom_out",
    "historical": "pan_left",
    "map": "pan_right",
    "document": "vertical_scan",
    "battle": "dramatic_zoom_in",
    "shocked": "dramatic_zoom_in",
    "nostalgic": "drift_float",
    "investigative": "slow_zoom_in",
    "conspiratorial": "slow_zoom_in",
    "haunted": "slow_zoom_in",
    "revelation": "dramatic_zoom_in",
    "default": "ken_burns",
}

# Built-in fallback catalog (subset of File 11 fields the engine needs).
BUILTIN_ANIMATIONS: Dict[str, Dict[str, Any]] = {
    "slow_zoom_in": {
        "zoom_start": 1.00,
        "zoom_end": 1.15,
        "pan_x": "center",
        "pan_y": "center",
        "easing": "ease_in_out",
    },
    "slow_zoom_out": {
        "zoom_start": 1.15,
        "zoom_end": 1.00,
        "pan_x": "center",
        "pan_y": "center",
        "easing": "ease_in_out",
    },
    "ken_burns": {
        "zoom_start": 1.00,
        "zoom_end": 1.20,
        "pan_x": "left_to_right",
        "pan_y": "top_to_bottom",
        "easing": "ease_in_out",
    },
    "pan_left": {
        "zoom_start": 1.10,
        "zoom_end": 1.10,
        "pan_x": "right_to_left",
        "pan_y": "center",
        "easing": "ease_in_out",
    },
    "pan_right": {
        "zoom_start": 1.10,
        "zoom_end": 1.10,
        "pan_x": "left_to_right",
        "pan_y": "center",
        "easing": "ease_in_out",
    },
    "pan_up": {
        "zoom_start": 1.10,
        "zoom_end": 1.10,
        "pan_x": "center",
        "pan_y": "bottom_to_top",
        "easing": "ease_in_out",
    },
    "pan_down": {
        "zoom_start": 1.10,
        "zoom_end": 1.10,
        "pan_x": "center",
        "pan_y": "top_to_bottom",
        "easing": "ease_in_out",
    },
    "diagonal_pan_tl_br": {
        "zoom_start": 1.15,
        "zoom_end": 1.15,
        "pan_x": "left_to_right",
        "pan_y": "top_to_bottom",
        "easing": "linear",
    },
    "dramatic_zoom_in": {
        "zoom_start": 1.00,
        "zoom_end": 1.40,
        "pan_x": "center",
        "pan_y": "center",
        "easing": "ease_out",
    },
    "pull_back": {
        "zoom_start": 1.35,
        "zoom_end": 1.00,
        "pan_x": "center",
        "pan_y": "center",
        "easing": "ease_in",
    },
    "vertical_scan": {
        "zoom_start": 1.15,
        "zoom_end": 1.15,
        "pan_x": "center",
        "pan_y": "top_to_bottom",
        "easing": "linear",
    },
    "drift_float": {
        "zoom_start": 1.05,
        "zoom_end": 1.10,
        "pan_x": "random_subtle",
        "pan_y": "random_subtle",
        "easing": "ease_in_out",
    },
    "static": {
        "zoom_start": 1.00,
        "zoom_end": 1.00,
        "pan_x": "center",
        "pan_y": "center",
        "easing": "none",
    },
}

_DIRECTIONAL_MODES = frozenset(
    {"left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top"}
)


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def _num(value: float) -> str:
    """Compact decimal for FFmpeg expressions."""
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


class AnimationEngine(BaseModule):
    """Generate FFmpeg zoompan filters and documentary animation choices."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize engine and load animation preset configuration."""
        super().__init__(container, MODULE_NAME)
        self._animations: Dict[str, Dict[str, Any]] = dict(BUILTIN_ANIMATIONS)
        self._weights: Dict[str, int] = dict(BUILTIN_DOCUMENTARY_WEIGHTS)
        self._intensity: Dict[str, float] = dict(BUILTIN_INTENSITY_MULTIPLIERS)
        self._mood_map: Dict[str, str] = dict(BUILTIN_MOOD_MAP)
        self._default = "ken_burns"
        self._rng = random.Random()
        self._load_config()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_zoompan_filter(
        self,
        animation_type: str,
        duration_seconds: float,
        fps: int = DEFAULT_FPS,
        intensity: str = "medium",
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
    ) -> Dict[str, Any]:
        """Build a complete zoompan filter string for FFmpeg.

        Args:
            animation_type: Preset id (ken_burns, slow_zoom_in, ...).
            duration_seconds: Scene length in seconds.
            fps: Output frames per second (default 30).
            intensity: subtle | medium | dramatic (scales zoom delta).
            width / height: Output frame size.

        Returns:
            Response with filter_string, animation_type, zoom_start/end,
            total_frames, easing, and applied intensity.
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="animation_engine is disabled")

        validated = self.validate_animation_settings(
            animation_type, intensity, duration_seconds, fps
        )
        data = validated["data"]
        warnings = list(validated.get("warnings") or [])
        atype = data["animation_type"]
        intensity = data["intensity"]
        duration = data["duration_seconds"]
        fps = data["fps"]

        total_frames = max(1, int(duration * fps))
        # validate_animation_settings guarantees atype exists in the catalog.
        preset = self._animations[atype]

        zoom_start, zoom_end = self._apply_intensity(preset, intensity)
        easing = str(preset.get("easing") or "ease_in_out")
        # FEATURE (v3.2.4): a single monotonic zoom/pan arc across a very
        # long hold (e.g. a 2-minute single-image scene) either finishes
        # early and then sits static, or moves so slowly it barely reads
        # as motion — feels frozen either way. For scenes over
        # LONG_HOLD_THRESHOLD_S, use an oscillating (breathing) progress
        # driver instead of one linear/eased arc: the same zoom/pan math
        # below is reused unchanged, just fed a progress value that cycles
        # a few times across the duration instead of moving one direction
        # once. Short scenes are completely unaffected (uses the original
        # single-arc easing exactly as before).
        if duration > LONG_HOLD_THRESHOLD_S:
            cycles = max(1, round(duration / LONG_HOLD_CYCLE_SECONDS))
            progress = self._oscillating_progress_expr(total_frames, cycles)
        else:
            progress = self._progress_expr(total_frames, easing)
        z_expr = self._zoom_expr(zoom_start, zoom_end, progress)
        x_expr = self._pan_expr(
            str(preset.get("pan_x") or "center"), "x", zoom_start, zoom_end, progress
        )
        y_expr = self._pan_expr(
            str(preset.get("pan_y") or "center"), "y", zoom_start, zoom_end, progress
        )

        filter_string = (
            f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}'"
            f":d={total_frames}:s={int(width)}x{int(height)}:fps={int(fps)}"
        )
        self.log.debug(
            "Generated zoompan for %s (%d frames): %s",
            atype,
            total_frames,
            filter_string,
        )
        return self.make_response(
            True,
            {
                "filter_string": filter_string,
                "animation_type": atype,
                "zoom_start": round(zoom_start, 6),
                "zoom_end": round(zoom_end, 6),
                "total_frames": total_frames,
                "fps": int(fps),
                "width": int(width),
                "height": int(height),
                "easing": easing,
                "intensity": intensity,
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def select_random_documentary_animation(self) -> Dict[str, Any]:
        """Select a weighted random documentary-style animation."""
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="animation_engine is disabled")
        choices = [
            a for a, w in self._weights.items() if w > 0 and a in self._animations
        ]
        weights = [self._weights[a] for a in choices]
        if not choices:
            return self.make_response(False, error="No weighted animations available")
        pick = self._rng.choices(choices, weights=weights, k=1)[0]
        return self.make_response(
            True,
            {"animation_type": pick, "weights": dict(self._weights)},
            duration_ms=_ms(started),
        )

    def get_animation_for_keyword_mood(self, mood: str) -> Dict[str, Any]:
        """Return the best animation for a detected keyword mood."""
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="animation_engine is disabled")
        key = str(mood or "default").strip().lower()
        animation = self._mood_map.get(key) or self._mood_map.get(
            "default", self._default
        )
        return self.make_response(
            True,
            {
                "animation_type": animation,
                "mood": key,
                "mapped": key in self._mood_map,
                "map_size": len(self._mood_map),
            },
            duration_ms=_ms(started),
        )

    def get_available_animations(self) -> Dict[str, Any]:
        """Return the animation catalog for UI display / validation."""
        started = time.perf_counter()
        catalog = []
        for aid, preset in self._animations.items():
            catalog.append(
                {
                    "id": aid,
                    "name": preset.get("name", aid),
                    "description": preset.get("description", ""),
                    "zoom_start": preset.get("zoom_start"),
                    "zoom_end": preset.get("zoom_end"),
                    "easing": preset.get("easing", "ease_in_out"),
                    "documentary_weight": self._weights.get(
                        aid, preset.get("documentary_weight", 0)
                    ),
                    "mood": preset.get("mood", []),
                    "best_for": preset.get("best_for", []),
                }
            )
        return self.make_response(
            True,
            {
                "animations": catalog,
                "count": len(catalog),
                "default_animation": self._default,
                "intensity_multipliers": dict(self._intensity),
            },
            duration_ms=_ms(started),
        )

    def validate_animation_settings(
        self,
        animation_type: str,
        intensity: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        fps: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Clamp/default animation settings, collecting warnings."""
        started = time.perf_counter()
        warnings: List[str] = []

        atype = str(animation_type or "").strip()
        if atype not in self._animations:
            fallback = (
                self._default
                if self._default in self._animations
                else next(iter(self._animations or {"ken_burns": {}}))
            )
            warnings.append(f"Unknown animation '{animation_type}', using '{fallback}'")
            atype = fallback

        inten = str(intensity or "medium").strip().lower()
        if inten not in self._intensity:
            warnings.append(f"Unknown intensity '{intensity}', using 'medium'")
            inten = "medium"

        dur = (
            MAX_DURATION
            if duration_seconds is None
            else self._as_float(duration_seconds, 0.0)
        )
        if duration_seconds is None or not (MIN_DURATION <= dur <= MAX_DURATION):
            if duration_seconds is not None:
                warnings.append(
                    f"Duration {duration_seconds}s clamped to [{MIN_DURATION}, {MAX_DURATION}]"
                )
            dur = min(MAX_DURATION, max(MIN_DURATION, dur))

        rate = DEFAULT_FPS if fps is None else int(self._as_float(fps, DEFAULT_FPS))
        if not (MIN_FPS <= rate <= MAX_FPS):
            warnings.append(f"FPS {fps} clamped to [{MIN_FPS}, {MAX_FPS}]")
            rate = min(MAX_FPS, max(MIN_FPS, rate))

        return self.make_response(
            True,
            {
                "animation_type": atype,
                "intensity": inten,
                "duration_seconds": round(dur, 3),
                "fps": rate,
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def generate_batch_filters(
        self,
        timeline: Any,
        default_intensity: str = "medium",
        fps: int = DEFAULT_FPS,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
    ) -> Dict[str, Any]:
        """Generate one zoompan filter per timeline scene.

        Per-scene animation source priority: explicit ``animation`` field
        (from file_parser) > scene ``mood`` via mood map > weighted random
        documentary pick.
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="animation_engine is disabled")

        if isinstance(timeline, dict):
            scenes = timeline.get("scenes") or timeline.get("scenes_data") or []
        elif isinstance(timeline, list):
            scenes = timeline
        else:
            return self.make_response(
                False, error="timeline must be a dict or list of scenes"
            )

        filters: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for index, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                warnings.append(f"Scene {index} is not a dict, skipped")
                continue
            atype = scene.get("animation") or scene.get("animation_type")
            if not atype:
                mood = scene.get("mood") or scene.get("keyword_mood")
                if mood:
                    atype = self.get_animation_for_keyword_mood(str(mood))["data"][
                        "animation_type"
                    ]
                else:
                    atype = self.select_random_documentary_animation()["data"][
                        "animation_type"
                    ]
            duration = scene.get("duration")
            if duration is None:
                try:
                    duration = float(scene.get("end_time", 0.0)) - float(
                        scene.get("start_time", 0.0)
                    )
                except (TypeError, ValueError):
                    duration = None
            result = self.get_zoompan_filter(
                str(atype),
                duration if duration else 8.0,
                fps=fps,
                intensity=str(scene.get("intensity") or default_intensity),
                width=width,
                height=height,
            )
            warnings.extend(
                f"Scene {index}: {w}" for w in (result.get("warnings") or [])
            )
            if result["success"]:
                filters.append(
                    {
                        "scene_index": index,
                        "scene_id": scene.get("id") or scene.get("scene_id") or index,
                        "animation_type": result["data"]["animation_type"],
                        "duration_seconds": result["data"]["total_frames"]
                        / result["data"]["fps"],
                        "filter_string": result["data"]["filter_string"],
                    }
                )
            else:
                warnings.append(
                    f"Scene {index}: filter generation failed ({result.get('error')})"
                )

        return self.make_response(
            True,
            {"filters": filters, "count": len(filters), "scenes_seen": len(scenes)},
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def apply_easing(self, t: float, mode: str = "ease_in_out") -> float:
        """Apply an easing curve to progress t in [0, 1]; unknown modes are linear."""
        t = min(1.0, max(0.0, self._as_float(t, 0.0)))
        mode = str(mode or "linear").strip().lower()
        if mode == "ease_in_out":
            return 0.5 * (1.0 - math.cos(math.pi * t))
        if mode == "ease_in":
            return 1.0 - math.cos(math.pi * t / 2.0)
        if mode == "ease_out":
            return math.sin(math.pi * t / 2.0)
        return t  # linear / none / unknown

    def is_optional_module(self) -> bool:
        """Return True — animation_engine may be disabled safely."""
        return True

    # ------------------------------------------------------------------
    # Expression builders
    # ------------------------------------------------------------------
    def _progress_expr(self, total_frames: int, easing: str) -> str:
        """FFmpeg expression for eased animation progress over output frames."""
        p = f"(on-1)/{max(1, total_frames - 1)}" if total_frames > 1 else "0"
        mode = str(easing or "linear").strip().lower()
        if mode == "ease_in_out":
            return f"0.5*(1-cos(PI*{p}))"
        if mode == "ease_in":
            return f"1-cos(PI*{p}/2)"
        if mode == "ease_out":
            return f"sin(PI*{p}/2)"
        if mode in ("linear", "none"):
            return p
        self.log.warning("Unknown easing '%s', using linear", easing)
        return p

    def _oscillating_progress_expr(self, total_frames: int, cycles: int) -> str:
        """Progress that breathes in and out `cycles` times, not once.

        Still a 0..1 value at every frame (same contract as
        _progress_expr) — fed straight into the existing zoom/pan math
        unchanged. Used only for long scene holds (v3.2.4).
        """
        if total_frames <= 1:
            return "0"
        p = f"(on-1)/{max(1, total_frames - 1)}"
        return f"0.5*(1-cos(2*PI*{max(1, cycles)}*{p}))"

    def _zoom_expr(self, zoom_start: float, zoom_end: float, progress: str) -> str:
        """Absolute zoom expression (first frame == zoom_start exactly)."""
        if progress == "0" or abs(zoom_end - zoom_start) < 1e-9:
            return _num(zoom_start)
        return f"{_num(zoom_start)}+({_num(zoom_end - zoom_start)})*{progress}"

    def _pan_expr(
        self, mode: str, axis: str, zoom_start: float, zoom_end: float, progress: str
    ) -> str:
        """Pan expression clamped to the valid crop window for the axis.

        Keeps File 07's intent (e.g. ~10% image travel at zoom 1.1) while
        guaranteeing the window never leaves the frame (no black edges).
        """
        size, center = ("iw", _CENTER_X) if axis == "x" else ("ih", _CENTER_Y)
        if mode == "center" or not mode:
            return center
        z_ref = max(zoom_start, zoom_end, 1.0)
        full = f"({size}-{size}/{_num(z_ref)})"  # max crop offset at peak zoom
        if mode == "random_subtle":
            start = self._rng.uniform(0.0, 0.5)
            end = self._rng.uniform(0.0, 0.5)
        elif mode in ("left_to_right", "top_to_bottom"):
            start, end = 0.0, 1.0
        elif mode in ("right_to_left", "bottom_to_top"):
            start, end = 1.0, 0.0
        else:
            self.log.warning("Unknown pan mode '%s', using center", mode)
            return center
        raw = f"{full}*({_num(start)}+({_num(end - start)})*{progress})"
        # Clamp into [0, size - size/zoom] so pans never exceed the frame.
        # Plain commas: the filter builder quotes each expression ('...').
        return f"min(max({raw},0),{size}-{size}/zoom)"

    def _apply_intensity(
        self, preset: Dict[str, Any], intensity: str
    ) -> Tuple[float, float]:
        """Scale the preset's zoom delta by the intensity multiplier."""
        zoom_start = self._as_float(preset.get("zoom_start"), 1.0)
        zoom_end = self._as_float(preset.get("zoom_end"), zoom_start)
        scale = self._intensity.get(intensity, 1.0)
        zoom_end = zoom_start + (zoom_end - zoom_start) * scale
        return (
            min(MAX_ZOOM, max(MIN_ZOOM, zoom_start)),
            min(MAX_ZOOM, max(MIN_ZOOM, zoom_end)),
        )

    def _load_config(self) -> None:
        """Load animation_presets.json via ConfigService with file fallback."""
        try:
            data = self.config.get_config("animation_presets")
        except Exception:  # noqa: BLE001
            data = {}
        if not data:
            path = Path("config/animation_presets.json")
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    self.log.error(
                        "animation_presets.json corrupt (%s); using built-ins", exc
                    )
                    return
        if not data:
            self.log.warning("animation_presets.json missing; using built-in presets")
            return

        animations = {
            str(a.get("id")): a for a in (data.get("animations") or []) if a.get("id")
        }
        if animations:
            self._animations = animations
        if isinstance(data.get("documentary_weights"), dict):
            self._weights = {
                str(k): int(v) for k, v in data["documentary_weights"].items()
            }
        if isinstance(data.get("intensity_multipliers"), dict):
            self._intensity = {
                str(k): float(v) for k, v in data["intensity_multipliers"].items()
            }
        if isinstance(data.get("mood_to_animation_map"), dict):
            self._mood_map = {
                str(k): str(v) for k, v in data["mood_to_animation_map"].items()
            }
        if data.get("default_animation"):
            self._default = str(data["default_animation"])

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        """Best-effort float conversion with fallback."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)
