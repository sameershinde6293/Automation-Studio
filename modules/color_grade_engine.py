"""Color grade engine: FFmpeg color-grading filtergraphs for scenes.

Optional BaseModule (can be disabled safely). Builds eq/colorbalance/vignette/
lut3d/noise/unsharp filter chains per preset so stills and video segments get
a consistent documentary look. Filter construction is pure string work (no
FFmpeg needed); ``apply_grade_to_image`` / ``apply_grade_to_video_segment``
run FFmpeg when available and degrade gracefully when it is not.

Spec sources: modules_specification.txt MODULE 11 (preset schema, algorithm)
and presets_and_configs.txt config/color_grade_presets.json (14 presets).
Dust/scratch overlays and LUT-opacity blending need a multi-input graph and
are deferred to export_engine (DEBT-B10a) — flagged via warnings/data.
"""

from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.service_container import BaseModule, ServiceContainer

MODULE_NAME = "color_grade_engine"

EQ_RANGES: Dict[str, tuple] = {
    "brightness": (-1.0, 1.0),
    "contrast": (0.0, 3.0),
    "saturation": (0.0, 3.0),
    "gamma": (0.1, 10.0),
}
UNIT_RANGES = (
    "vignette_strength",
    "film_grain_amount",
    "lut_opacity",
    "sharpen_amount",
)
MAX_VIGNETTE_ANGLE = math.pi / 2.0

# File 07 MODULE 11 fallback subset (schema mirrors File 11 presets).
BUILTIN_PRESETS: Dict[str, Dict[str, Any]] = {
    "dark_moody": {
        "name": "Dark Moody",
        "genre_match": ["dark_history", "mystery", "horror_documentary"],
        "ffmpeg_eq": "brightness=-0.05:contrast=1.20:saturation=0.70:gamma=0.90",
        "ffmpeg_colorbalance": "rs=-0.10:gs=-0.05:bs=0.10:rm=-0.05:gm=0.00:bm=0.05:rh=-0.05:gh=0.00:bh=0.05",
        "vignette_enabled": True,
        "vignette_strength": 0.70,
        "vignette_angle": round(math.pi / 4, 3),
        "film_grain_enabled": True,
        "film_grain_amount": 0.04,
        "lut_file": "dark_moody.cube",
        "lut_opacity": 0.80,
        "sharpen_enabled": True,
        "sharpen_amount": 0.3,
    },
    "clean_modern": {
        "name": "Clean Modern",
        "genre_match": [],
        "ffmpeg_eq": "brightness=0:contrast=1:saturation=1:gamma=1",
        "ffmpeg_colorbalance": "",
        "vignette_enabled": False,
        "vignette_strength": 0.0,
        "vignette_angle": 0.0,
        "film_grain_enabled": False,
        "film_grain_amount": 0.0,
        "lut_file": None,
        "lut_opacity": 0.0,
        "sharpen_enabled": False,
        "sharpen_amount": 0.0,
    },
}
BUILTIN_DEFAULT = "dark_moody"
FFMPEG_TIMEOUT = 120


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def _num(value: float) -> str:
    """Compact decimal for FFmpeg expressions."""
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


class ColorGradeEngine(BaseModule):
    """Build FFmpeg color-grade filtergraphs and apply them via FFmpeg."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize engine and load color-grade preset configuration."""
        super().__init__(container, MODULE_NAME)
        self._presets: Dict[str, Dict[str, Any]] = dict(BUILTIN_PRESETS)
        self._default = BUILTIN_DEFAULT
        self._load_config()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def build_grade_filter(
        self, preset_name: str, custom_settings: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Build a complete FFmpeg filtergraph string for a color grade.

        Chain order (File 07): eq -> colorbalance -> vignette -> lut3d
        (only if the LUT file exists) -> noise grain -> unsharp.

        Args:
            preset_name: Preset id from color_grade_presets.json.
            custom_settings: Optional overrides (eq params, vignette_*, etc.).

        Returns:
            Response with filtergraph, preset used, and component list.
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="color_grade_engine is disabled")

        validated = self.validate_grade_settings(preset_name, custom_settings)
        warnings = list(validated.get("warnings") or [])
        vdata = validated["data"]
        preset = dict(vdata["preset"])
        pname = vdata["preset_name"]

        components: List[str] = []
        eq_opts = self._parse_eq(preset.get("ffmpeg_eq") or "")
        if eq_opts:
            components.append(
                "eq=" + ":".join(f"{k}={_num(v)}" for k, v in eq_opts.items())
            )

        colorbalance = str(preset.get("ffmpeg_colorbalance") or "").strip()
        if colorbalance:
            components.append("colorbalance=" + colorbalance)

        if (
            preset.get("vignette_enabled")
            and float(preset.get("vignette_strength") or 0.0) > 0
        ):
            angle = preset.get("vignette_angle")
            angle = (
                float(angle)
                if angle
                else float(preset["vignette_strength"]) * math.pi / 4.0
            )
            components.append(
                f"vignette={_num(min(angle, MAX_VIGNETTE_ANGLE))}:eval=frame"
            )

        lut_applied = False
        lut_file = preset.get("lut_file")
        if lut_file:
            lut_path = self._luts_folder() / str(lut_file)
            if lut_path.exists():
                components.append(f"lut3d='{lut_path.as_posix()}'")
                lut_applied = True
            else:
                warnings.append(
                    f"LUT not found: {lut_path} — grade built without lut3d"
                )

        if (
            preset.get("film_grain_enabled")
            and float(preset.get("film_grain_amount") or 0.0) > 0
        ):
            components.append(
                f"noise=c0s={int(float(preset['film_grain_amount']) * 100)}:c0f=t+u"
            )

        if (
            preset.get("sharpen_enabled")
            and float(preset.get("sharpen_amount") or 0.0) > 0
        ):
            amount = float(preset["sharpen_amount"])
            components.append(f"unsharp=5:5:{_num(amount)}:5:5:0.0")

        if preset.get("dust_overlay_enabled") or preset.get("scratch_overlay_enabled"):
            warnings.append(
                "Dust/scratch overlays deferred to export_engine (DEBT-B10a)"
            )

        filtergraph = ",".join(components)
        return self.make_response(
            True,
            {
                "filtergraph": filtergraph,
                "preset_name": pname,
                "components": components,
                "lut_applied": lut_applied,
                "lut_opacity_pending": bool(
                    lut_applied and float(preset.get("lut_opacity") or 0) < 0.99
                ),
                "sharpen_applied": any(c.startswith("unsharp") for c in components),
                "grain_applied": any(c.startswith("noise") for c in components),
                "vignette_applied": any(c.startswith("vignette") for c in components),
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def apply_grade_to_image(
        self, image_path: str | Path, preset_name: str, output_path: str | Path
    ) -> Dict[str, Any]:
        """Apply a color grade to a single image using FFmpeg."""
        command_extra = ["-frames:v", "1"]
        return self._apply_grade(
            image_path, preset_name, output_path, command_extra, audio_copy=False
        )

    def apply_grade_to_video_segment(
        self, video_path: str | Path, preset_name: str, output_path: str | Path
    ) -> Dict[str, Any]:
        """Apply a color grade to a video segment (audio copied through)."""
        return self._apply_grade(
            video_path, preset_name, output_path, ["-c:a", "copy"], audio_copy=True
        )

    def add_film_grain(self, filtergraph: str, amount: float) -> Dict[str, Any]:
        """Append a film-grain (noise) filter to a filtergraph."""
        started = time.perf_counter()
        try:
            amount_f = max(0.0, min(1.0, float(amount)))
        except (TypeError, ValueError):
            amount_f = 0.0
        if amount_f <= 0:
            return self.make_response(
                True,
                {"filtergraph": filtergraph, "grain_added": False},
                duration_ms=_ms(started),
            )
        noise = f"noise=c0s={int(amount_f * 100)}:c0f=t+u"
        graph = f"{filtergraph},{noise}" if filtergraph else noise
        return self.make_response(
            True,
            {"filtergraph": graph, "grain_added": True, "grain_filter": noise},
            duration_ms=_ms(started),
        )

    def add_vignette(self, filtergraph: str, strength: float) -> Dict[str, Any]:
        """Append a vignette filter whose angle derives from strength (File 07)."""
        started = time.perf_counter()
        try:
            strength_f = max(0.0, min(1.0, float(strength)))
        except (TypeError, ValueError):
            strength_f = 0.0
        if strength_f <= 0:
            return self.make_response(
                True,
                {"filtergraph": filtergraph, "vignette_added": False},
                duration_ms=_ms(started),
            )
        angle = min(strength_f * math.pi / 4.0, MAX_VIGNETTE_ANGLE)
        vignette = f"vignette={_num(angle)}:eval=frame"
        graph = f"{filtergraph},{vignette}" if filtergraph else vignette
        return self.make_response(
            True,
            {"filtergraph": graph, "vignette_added": True, "angle": round(angle, 6)},
            duration_ms=_ms(started),
        )

    def get_available_presets(self) -> Dict[str, Any]:
        """Return the grade catalog for UI display / validation."""
        started = time.perf_counter()
        catalog = [
            {
                "id": pid,
                "name": p.get("name", pid),
                "description": p.get("description", ""),
                "genre_match": p.get("genre_match", []),
                "has_lut": bool(p.get("lut_file")),
                "color_temperature": p.get("color_temperature", ""),
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

    def get_preset_for_genre(self, genre: str) -> Dict[str, Any]:
        """Resolve the best grade preset for a genre.

        Order: documentary_genres.json color_grade -> preset genre_match -> default.
        """
        started = time.perf_counter()
        key = str(genre or "").strip()
        preset, source = self._default, "default"
        if key:
            genres_cfg = self._safe_get_config("documentary_genres")
            for g in genres_cfg.get("genres") or []:
                mapped = g.get("color_grade") or g.get("default_color_grade")
                if str(g.get("id")) == key and str(mapped) in self._presets:
                    preset, source = str(mapped), "documentary_genres"
                    break
            else:
                for pid, p in self._presets.items():
                    if key in (p.get("genre_match") or []):
                        preset, source = pid, "genre_match"
                        break
        return self.make_response(
            True,
            {"preset_name": preset, "genre": key, "source": source},
            duration_ms=_ms(started),
        )

    def validate_grade_settings(
        self, preset_name: str, custom_settings: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Resolve a preset, clamp overrides to FFmpeg-safe ranges, collect warnings."""
        started = time.perf_counter()
        warnings: List[str] = []
        pname = str(preset_name or "").strip()
        if pname not in self._presets:
            fallback = (
                self._default
                if self._default in self._presets
                else next(iter(self._presets))
            )
            warnings.append(f"Unknown grade preset '{preset_name}', using '{fallback}'")
            pname = fallback
        preset = dict(self._presets[pname])

        for key, value in (custom_settings or {}).items():
            if key in EQ_RANGES:
                lo, hi = EQ_RANGES[key]
                raw = self._as_float(value, lo)
                clamped = min(hi, max(lo, raw))
                if clamped != raw:
                    warnings.append(f"{key} {value} clamped to [{lo}, {hi}]")
                eq_opts = self._parse_eq(preset.get("ffmpeg_eq") or "")
                eq_opts[key] = clamped
                preset["ffmpeg_eq"] = ":".join(
                    f"{k}={_num(v)}" for k, v in eq_opts.items()
                )
            elif key in UNIT_RANGES:
                clamped = min(1.0, max(0.0, self._as_float(value, 0.0)))
                if clamped != value:
                    warnings.append(f"{key} {value} clamped to [0, 1]")
                preset[key] = clamped
                if key == "vignette_strength":
                    preset["vignette_enabled"] = clamped > 0
                if key == "film_grain_amount":
                    preset["film_grain_enabled"] = clamped > 0
                if key == "sharpen_amount":
                    preset["sharpen_enabled"] = clamped > 0
            elif key == "vignette_angle":
                clamped = max(0.0, min(MAX_VIGNETTE_ANGLE, self._as_float(value, 0.0)))
                preset[key] = clamped
            else:
                warnings.append(f"Ignoring unknown grade override '{key}'")

        return self.make_response(
            True,
            {"preset_name": pname, "preset": preset},
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def is_optional_module(self) -> bool:
        """Return True — color_grade_engine may be disabled safely."""
        return True

    # ------------------------------------------------------------------
    # FFmpeg execution
    # ------------------------------------------------------------------
    def _apply_grade(
        self,
        input_path: str | Path,
        preset_name: str,
        output_path: str | Path,
        command_extra: List[str],
        audio_copy: bool,
    ) -> Dict[str, Any]:
        """Shared FFmpeg runner for image/video grading (Rules 3/4/7)."""
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="color_grade_engine is disabled")
        source = Path(input_path)
        if not source.exists():
            self.log.error("Input not found: %s", source)
            return self.make_response(
                False, error=f"Input file not found: {input_path}"
            )

        ffmpeg = self.hardware.find_ffmpeg() if self.hardware else None
        if not ffmpeg:
            return self.make_response(
                False, error="FFmpeg not available — cannot apply grade"
            )

        built = self.build_grade_filter(preset_name, {})
        if not built["success"]:
            return built
        graph = built["data"]["filtergraph"]

        command = [
            str(ffmpeg),
            "-y",
            "-i",
            str(source),
            "-vf",
            graph,
            *command_extra,
            str(output_path),
        ]
        self.log.info("FFmpeg grade command: %s", " ".join(command))
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.log.error("FFmpeg run failed: %s", exc)
            return self.make_response(False, error=str(exc), duration_ms=_ms(started))
        if result.returncode != 0:
            self.log.error(
                "FFmpeg error: %s", result.stderr[-500:] if result.stderr else "unknown"
            )
            return self.make_response(
                False,
                error=(result.stderr or "ffmpeg failed")[-300:],
                duration_ms=_ms(started),
            )

        return self.make_response(
            True,
            {
                "output_path": str(output_path),
                "preset_name": built["data"]["preset_name"],
                "filtergraph": graph,
                "audio_copied": audio_copy,
            },
            warnings=list(built.get("warnings") or []),
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Config & helpers
    # ------------------------------------------------------------------
    def _load_config(self) -> None:
        """Load color_grade_presets.json via ConfigService with file fallback."""
        data = self._safe_get_config("color_grade_presets")
        if not data:
            path = Path("config/color_grade_presets.json")
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    self.log.error(
                        "color_grade_presets.json corrupt (%s); using built-ins", exc
                    )
                    return
        if not data:
            self.log.warning("color_grade_presets.json missing; using built-in presets")
            return
        presets = {
            str(p.get("id")): p for p in (data.get("presets") or []) if p.get("id")
        }
        if presets:
            self._presets = presets
        if data.get("default_preset"):
            self._default = str(data["default_preset"])

    def _safe_get_config(self, name: str) -> Dict[str, Any]:
        """ConfigService lookup that never raises."""
        try:
            data = self.config.get_config(name)
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    def _luts_folder(self) -> Path:
        """LUT folder from settings with project default (Rule 3)."""
        try:
            configured = self.config.get("luts_folder")
        except Exception:  # noqa: BLE001
            configured = None
        return Path(str(configured)) if configured else Path("assets") / "luts"

    @staticmethod
    def _parse_eq(eq_string: str) -> Dict[str, float]:
        """Parse 'brightness=-0.05:contrast=1.2' into an ordered dict."""
        opts: Dict[str, float] = {}
        for part in eq_string.split(":"):
            if "=" not in part:
                continue
            key, _, value = part.partition("=")
            try:
                opts[key.strip()] = float(value)
            except ValueError:
                continue
        return {
            k: opts[k]
            for k in ("brightness", "contrast", "saturation", "gamma")
            if k in opts
        }

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        """Best-effort float conversion with fallback."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)
