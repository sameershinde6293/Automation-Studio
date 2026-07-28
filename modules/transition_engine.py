"""Transition engine: FFmpeg xfade filters and smart transition selection.

Required BaseModule for render. Maps Autopilot transition names to FFmpeg
xfade transitions and optional custom filter chains.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.service_container import BaseModule, ServiceContainer

MODULE_NAME = "transition_engine"

# Autopilot name -> FFmpeg xfade transition name (None = hard cut)
XFADE_MAP: Dict[str, Optional[str]] = {
    # BASIC
    "fade": "fade",
    "fade_black": "fadeblack",
    "fade_white": "fadewhite",
    "crossfade": "fade",
    "dissolve": "dissolve",
    "hard_cut": None,
    "slide_left": "slideleft",
    "slide_right": "slideright",
    # CINEMATIC
    "fade_up": "wipeup",
    "fade_down": "wipedown",
    "iris_open": "circlecrop",
    "iris_close": "rectcrop",
    "circle_open": "circleopen",
    "circle_close": "circleclose",
    "pixelize": "pixelize",
    "wipe_diagonal": "wipetl",
    # DRAMATIC
    "dramatic_flash": "fadewhite",
    "whip_pan": "slideleft",
    "zoom_in": "smoothleft",
    "zoom_out": "smoothright",
    "burn_out": "fadeblack",
    "shake_transition": "distance",
    "glitch": "pixelize",
    # ARTISTIC
    "ink_bleed": "dissolve",
    "film_burn": "fadewhite",
    "light_leak": "smoothright",
    "page_turn": "wipeleft",
    "old_film": "dissolve",
    "tv_static": "pixelize",
    "water_ripple": "smoothright",
    "matrix_dissolve": "pixelize",
    # aliases from timeline / scripts
    "dip_to_black": "fadeblack",
    "dip_to_white": "fadewhite",
    "cut": None,
}

CATEGORIES: Dict[str, List[str]] = {
    "basic": [
        "fade",
        "fade_black",
        "fade_white",
        "crossfade",
        "dissolve",
        "hard_cut",
        "slide_left",
        "slide_right",
    ],
    "cinematic": [
        "fade_up",
        "fade_down",
        "iris_open",
        "iris_close",
        "circle_open",
        "circle_close",
        "pixelize",
        "wipe_diagonal",
    ],
    "dramatic": [
        "dramatic_flash",
        "whip_pan",
        "zoom_in",
        "zoom_out",
        "burn_out",
        "shake_transition",
        "glitch",
    ],
    "artistic": [
        "ink_bleed",
        "film_burn",
        "light_leak",
        "page_turn",
        "old_film",
        "tv_static",
        "water_ripple",
        "matrix_dissolve",
    ],
}

# Default durations by type when not specified
DEFAULT_DURATIONS: Dict[str, float] = {
    "hard_cut": 0.0,
    "cut": 0.0,
    "dramatic_flash": 0.25,
    "whip_pan": 0.35,
    "glitch": 0.4,
    "crossfade": 1.0,
    "dissolve": 0.8,
    "fade_black": 1.0,
    "fade_white": 0.8,
    "fade": 0.8,
}

MIN_DURATION = 0.1
MAX_DURATION = 5.0
RECOMMENDED_MIN = 0.5
RECOMMENDED_MAX = 3.0

# Mood pair preferences for smart selection
MOOD_TRANSITIONS: Dict[Tuple[str, str], List[str]] = {
    ("dark", "dark"): ["crossfade", "fade_black", "dissolve", "old_film"],
    ("dark", "bright"): ["fade_white", "dramatic_flash", "light_leak"],
    ("bright", "dark"): ["fade_black", "burn_out", "dissolve"],
    ("same", "same"): ["dissolve", "fade", "crossfade"],
    ("historical", "any"): ["film_burn", "old_film", "dissolve", "fade_black"],
    ("mystery", "any"): ["fade_black", "dissolve", "iris_close", "pixelize"],
    ("action", "any"): ["whip_pan", "shake_transition", "slide_left", "glitch"],
    ("emotional", "any"): ["fade_black", "dissolve", "fade", "crossfade"],
}

DARK_MOODS = frozenset(
    {
        "dark",
        "ominous",
        "solemn",
        "haunted",
        "dramatic",
        "conspiratorial",
        "cold",
        "melancholic",
        "sad",
        "fearful",
    }
)
BRIGHT_MOODS = frozenset(
    {
        "excited",
        "warm",
        "compassionate",
        "nostalgic",
        "calm",
        "reverent",
    }
)
HISTORICAL_MOODS = frozenset({"historical", "authoritative", "solemn", "reverent"})
MYSTERY_MOODS = frozenset({"mysterious", "ominous", "conspiratorial", "haunted"})
ACTION_MOODS = frozenset({"urgent", "angry", "tense", "shocked", "accusatory"})


class TransitionEngine(BaseModule):
    """Generate FFmpeg transition filters and smart selections."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize transition engine and recent-history tracker."""
        super().__init__(container, MODULE_NAME)
        self._recent: List[str] = []
        self._config_presets: Dict[str, Dict[str, Any]] = {}
        self._load_config_presets()

    def build_transition_filter(
        self,
        scene_a: Dict[str, Any],
        scene_b: Dict[str, Any],
        transition_type: str,
        duration: Optional[float] = None,
        input_a: str = "0:v",
        input_b: str = "1:v",
        output_label: str = "v",
    ) -> Dict[str, Any]:
        """Build FFmpeg xfade (or custom) filter for a scene pair.

        Args:
            scene_a: Previous scene with end_time.
            scene_b: Next scene.
            transition_type: Autopilot transition id.
            duration: Optional override seconds.
            input_a / input_b: FFmpeg stream labels.
            output_label: Output stream label.

        Returns:
            Response with filter_string, ffmpeg_name, offset, duration, is_hard_cut.
        """
        started = time.perf_counter()
        validated = self.validate_transition_settings(transition_type, duration)
        ttype = str(validated["data"]["transition_type"])
        dur = float(validated["data"]["duration"])
        warnings = list(validated.get("warnings") or [])

        ffmpeg_name = XFADE_MAP.get(ttype, "fade")
        end_a = float(scene_a.get("end_time") or scene_a.get("end") or 0.0)
        offset = max(0.0, end_a - dur) if dur > 0 else end_a

        if ffmpeg_name is None or ttype in ("hard_cut", "cut") or dur <= 0:
            return self.make_response(
                True,
                {
                    "filter_string": None,
                    "filter_complex": None,
                    "ffmpeg_name": None,
                    "transition_type": ttype,
                    "duration": 0.0,
                    "offset": round(end_a, 3),
                    "is_hard_cut": True,
                    "extra_filters": [],
                },
                warnings=warnings,
                duration_ms=_ms(started),
            )

        # Standard xfade
        xfade = (
            f"[{input_a}][{input_b}]xfade=transition={ffmpeg_name}:"
            f"duration={dur:.3f}:offset={offset:.3f}[{output_label}]"
        )
        extra = self._custom_effect_filters(ttype, dur)

        # Optionally wrap with pre/post effects on streams
        filter_complex = xfade
        if extra:
            # Apply extras after xfade on output label
            chain = ",".join(extra)
            filter_complex = f"{xfade};[{output_label}]{chain}[{output_label}fx]"
            out_label = f"{output_label}fx"
        else:
            out_label = output_label

        return self.make_response(
            True,
            {
                "filter_string": (
                    f"xfade=transition={ffmpeg_name}:duration={dur:.3f}:offset={offset:.3f}"
                ),
                "filter_complex": filter_complex,
                "ffmpeg_name": ffmpeg_name,
                "transition_type": ttype,
                "duration": round(dur, 3),
                "offset": round(offset, 3),
                "is_hard_cut": False,
                "extra_filters": extra,
                "output_label": out_label,
                "scene_a_end": end_a,
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def get_available_transitions(self) -> Dict[str, Any]:
        """Return all supported transitions categorized."""
        started = time.perf_counter()
        catalog = []
        for category, names in CATEGORIES.items():
            for name in names:
                catalog.append(
                    {
                        "id": name,
                        "category": category,
                        "ffmpeg": XFADE_MAP.get(name),
                        "default_duration": DEFAULT_DURATIONS.get(name, 1.0),
                        "is_hard_cut": XFADE_MAP.get(name) is None,
                    }
                )
        return self.make_response(
            True,
            {
                "transitions": catalog,
                "categories": {k: list(v) for k, v in CATEGORIES.items()},
                "count": len(catalog),
            },
            duration_ms=_ms(started),
        )

    def smart_transition_selection(
        self,
        scene_a: Dict[str, Any],
        scene_b: Dict[str, Any],
        mood: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Choose a transition based on moods and anti-repeat history."""
        started = time.perf_counter()
        mood_a = str(
            scene_a.get("keyword_mood") or scene_a.get("mood") or "neutral"
        ).lower()
        mood_b = str(
            mood or scene_b.get("keyword_mood") or scene_b.get("mood") or "neutral"
        ).lower()

        candidates = self._candidates_for_moods(mood_a, mood_b)
        # Avoid last 2 repeats if possible
        filtered = [c for c in candidates if c not in self._recent[-2:]]
        if not filtered:
            filtered = candidates
        # Avoid 3 identical in a row
        if len(self._recent) >= 3 and len(set(self._recent[-3:])) == 1:
            same = self._recent[-1]
            filtered = [c for c in filtered if c != same] or filtered

        choice = filtered[0]
        # Round-robin variety within list
        if len(filtered) > 1:
            idx = len(self._recent) % len(filtered)
            choice = filtered[idx]

        self._recent.append(choice)
        if len(self._recent) > 20:
            self._recent = self._recent[-20:]

        duration = DEFAULT_DURATIONS.get(choice, 1.0)
        return self.make_response(
            True,
            {
                "transition_type": choice,
                "duration": duration,
                "mood_a": mood_a,
                "mood_b": mood_b,
                "candidates": candidates,
            },
            duration_ms=_ms(started),
        )

    def validate_transition_settings(
        self,
        transition_type: str,
        duration: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Validate/correct transition type and duration."""
        started = time.perf_counter()
        warnings: List[str] = []
        ttype = str(transition_type or "crossfade").strip().lower().replace("-", "_")
        ttype = ttype.replace(" ", "_")
        if ttype not in XFADE_MAP:
            warnings.append(
                f"Unknown transition '{transition_type}' — defaulting to crossfade"
            )
            ttype = "crossfade"

        if duration is None:
            dur = float(DEFAULT_DURATIONS.get(ttype, 1.0))
        else:
            try:
                dur = float(duration)
            except (TypeError, ValueError):
                warnings.append("Invalid duration — using default")
                dur = float(DEFAULT_DURATIONS.get(ttype, 1.0))

        if ttype in ("hard_cut", "cut"):
            dur = 0.0
        else:
            if dur < MIN_DURATION:
                warnings.append(
                    f"Duration {dur}s too short — raised to {MIN_DURATION}s"
                )
                dur = MIN_DURATION
            if dur > MAX_DURATION:
                warnings.append(f"Duration {dur}s too long — capped to {MAX_DURATION}s")
                dur = MAX_DURATION
            if dur < RECOMMENDED_MIN or dur > RECOMMENDED_MAX:
                warnings.append(
                    f"Duration {dur}s outside recommended {RECOMMENDED_MIN}-{RECOMMENDED_MAX}s"
                )

        return self.make_response(
            True,
            {
                "transition_type": ttype,
                "duration": round(dur, 3),
                "valid": True,
                "ffmpeg_name": XFADE_MAP.get(ttype),
            },
            warnings=warnings,
            duration_ms=_ms(started),
        )

    def generate_batch_filters(
        self,
        timeline: Dict[str, Any],
        use_smart: bool = False,
    ) -> Dict[str, Any]:
        """Generate transition filters for every consecutive scene pair."""
        started = time.perf_counter()
        scenes = list(timeline.get("scenes") or [])
        filters: List[Dict[str, Any]] = []
        if len(scenes) < 2:
            return self.make_response(
                True,
                {"filters": [], "count": 0},
                warnings=["Fewer than 2 scenes — no transitions"],
                duration_ms=_ms(started),
            )

        for index in range(len(scenes) - 1):
            a = scenes[index]
            b = scenes[index + 1]
            if use_smart:
                smart = self.smart_transition_selection(a, b)
                ttype = smart["data"]["transition_type"]
                duration = smart["data"]["duration"]
            else:
                ttype = str(
                    a.get("transition_out")
                    or (a.get("transition_out") or {}).get("type")
                    or b.get("transition_in")
                    or "crossfade"
                )
                if isinstance(a.get("transition_out"), dict):
                    ttype = str(a["transition_out"].get("type") or ttype)
                    duration = float(a["transition_out"].get("duration") or 1.0)
                else:
                    duration = float(
                        a.get("transition_duration")
                        or DEFAULT_DURATIONS.get(str(ttype).lower(), 1.0)
                    )
            built = self.build_transition_filter(a, b, ttype, duration)
            filters.append(
                {
                    "index": index,
                    "from_scene": a.get("scene_number") or a.get("id"),
                    "to_scene": b.get("scene_number") or b.get("id"),
                    **built["data"],
                    "warnings": built.get("warnings") or [],
                }
            )

        return self.make_response(
            True,
            {
                "filters": filters,
                "count": len(filters),
                "hard_cuts": sum(1 for f in filters if f.get("is_hard_cut")),
            },
            duration_ms=_ms(started),
        )

    def reset_history(self) -> None:
        """Clear smart-selection recent history (for tests)."""
        self._recent.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_config_presets(self) -> None:
        """Load optional transition_presets.json for duration defaults."""
        try:
            data = self.config.get_config("transition_presets")
        except Exception:  # noqa: BLE001
            data = {}
        if not data:
            path = Path("config/transition_presets.json")
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = {}
        for preset in data.get("presets") or []:
            pid = str(preset.get("id") or "")
            if pid:
                self._config_presets[pid] = preset
                if pid in DEFAULT_DURATIONS or True:
                    try:
                        DEFAULT_DURATIONS[pid] = float(preset.get("duration", 1.0))
                    except (TypeError, ValueError):
                        pass

    def _candidates_for_moods(self, mood_a: str, mood_b: str) -> List[str]:
        """Pick candidate transitions for a mood pair."""
        a_dark = mood_a in DARK_MOODS
        b_dark = mood_b in DARK_MOODS
        a_bright = mood_a in BRIGHT_MOODS
        b_bright = mood_b in BRIGHT_MOODS

        # Contrast pairs first (dark↔bright) before genre buckets
        if a_dark and b_bright:
            return list(MOOD_TRANSITIONS[("dark", "bright")])
        if a_bright and b_dark:
            return list(MOOD_TRANSITIONS[("bright", "dark")])
        if a_dark and b_dark:
            return list(MOOD_TRANSITIONS[("dark", "dark")])
        if mood_a in ACTION_MOODS or mood_b in ACTION_MOODS:
            return list(MOOD_TRANSITIONS[("action", "any")])
        if mood_a in HISTORICAL_MOODS or mood_b in HISTORICAL_MOODS:
            return list(MOOD_TRANSITIONS[("historical", "any")])
        if mood_a in MYSTERY_MOODS or mood_b in MYSTERY_MOODS:
            return list(MOOD_TRANSITIONS[("mystery", "any")])
        if mood_a == mood_b:
            return list(MOOD_TRANSITIONS[("same", "same")])
        return list(MOOD_TRANSITIONS[("same", "same")]) + ["crossfade", "dissolve"]

    def _custom_effect_filters(self, ttype: str, duration: float) -> List[str]:
        """Optional post-xfade stylistic filters for artistic transitions."""
        if ttype == "burn_out":
            return ["eq=saturation=1.3:brightness=0.05", "colorbalance=rs=0.1:gs=0.05"]
        if ttype == "film_burn":
            return ["eq=saturation=0.85:contrast=1.1", "noise=alls=12:allf=t"]
        if ttype == "old_film":
            return ["eq=saturation=0.6:contrast=1.15", "noise=alls=18:allf=t"]
        if ttype == "tv_static":
            return ["noise=alls=40:allf=t", "eq=contrast=1.2"]
        if ttype == "matrix_dissolve":
            return ["colorchannelmixer=rr=0.2:rg=0.8:rb=0.2:gr=0.1:gg=1:gb=0.1"]
        if ttype == "glitch":
            return ["noise=alls=25:allf=t"]
        if ttype == "light_leak":
            return ["eq=brightness=0.08:saturation=1.15"]
        if ttype == "ink_bleed":
            return ["eq=contrast=1.2:saturation=0.9"]
        return []


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)
