"""Unit tests for modules.animation_engine.AnimationEngine.

No real FFmpeg render required — zoompan string generation only
(same pattern as tests for transition_engine).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from core.service_container import ServiceContainer
from modules.animation_engine import AnimationEngine

CATALOG_SIZE = 13  # File 11 animation_presets.json


@pytest.fixture
def container(project_root: Path, tmp_path: Path) -> ServiceContainer:
    """Isolated container with the real project config folder."""
    return ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp_path / "autopilot.db"),
            "schema_path": str(project_root / "database" / "schema.sql"),
            "config_folder": str(project_root / "config"),
            "cache_folder": str(tmp_path / "cache"),
            "log_folder": str(tmp_path / "logs"),
            "ffmpeg_path": "ffmpeg",
        },
        project_root=project_root,
    )


@pytest.fixture
def engine(container: ServiceContainer) -> AnimationEngine:
    """Animation engine instance with seeded RNG (deterministic tests)."""
    eng = AnimationEngine(container)
    eng._rng.seed(1337)
    return eng


class TestCatalog:
    """Preset catalog completeness and metadata."""

    def test_all_preset_ids_load(self, engine: AnimationEngine) -> None:
        result = engine.get_available_animations()
        assert result["success"] is True
        ids = {a["id"] for a in result["data"]["animations"]}
        expected = {
            # File 07 core ten
            "slow_zoom_in",
            "slow_zoom_out",
            "ken_burns",
            "pan_left",
            "pan_right",
            "pan_up",
            "pan_down",
            "static",
            "diagonal_pan_tl_br",
            "dramatic_zoom_in",
            # File 11 extended three
            "pull_back",
            "vertical_scan",
            "drift_float",
        }
        assert expected <= ids
        assert len(ids) == CATALOG_SIZE
        assert result["data"]["count"] == CATALOG_SIZE
        assert result["data"]["default_animation"] == "ken_burns"
        assert result["data"]["intensity_multipliers"] == {
            "subtle": 0.50,
            "medium": 1.00,
            "dramatic": 1.50,
        }


class TestFilterGeneration:
    """zoompan filter-string structure."""

    def test_ken_burns_filter_shape(self, engine: AnimationEngine) -> None:
        result = engine.get_zoompan_filter("ken_burns", 8.0, 30, "medium")
        assert result["success"] is True
        f = result["data"]["filter_string"]
        assert f.startswith("zoompan=")
        assert "d=240" in f
        assert "fps=30" in f
        assert "s=1920x1080" in f

    def test_zoom_direction_in_vs_out(self, engine: AnimationEngine) -> None:
        zin = engine.get_zoompan_filter("slow_zoom_in", 5.0, 30, "medium")["data"]
        zout = engine.get_zoompan_filter("slow_zoom_out", 5.0, 30, "medium")["data"]
        assert zin["zoom_start"] < zin["zoom_end"]  # zooms in
        assert zout["zoom_start"] > zout["zoom_end"]  # zooms out
        assert zin["filter_string"] != zout["filter_string"]

    def test_pan_left_right_differ(self, engine: AnimationEngine) -> None:
        left = engine.get_zoompan_filter("pan_left", 5.0, 30, "medium")["data"][
            "filter_string"
        ]
        right = engine.get_zoompan_filter("pan_right", 5.0, 30, "medium")["data"][
            "filter_string"
        ]
        assert left != right
        # Both pans must be clamped so the crop never leaves the frame.
        assert "min(max(" in left and "min(max(" in right

    def test_static_has_no_motion(self, engine: AnimationEngine) -> None:
        data = engine.get_zoompan_filter("static", 5.0, 30, "medium")["data"]
        assert data["zoom_start"] == pytest.approx(data["zoom_end"])
        assert "cos" not in data["filter_string"]  # easing "none" -> linear math-free

    def test_intensity_scales_zoom_delta(self, engine: AnimationEngine) -> None:
        subtle = engine.get_zoompan_filter("slow_zoom_in", 5.0, 30, "subtle")["data"]
        dramatic = engine.get_zoompan_filter("slow_zoom_in", 5.0, 30, "dramatic")[
            "data"
        ]
        delta_subtle = abs(subtle["zoom_end"] - subtle["zoom_start"])
        delta_dramatic = abs(dramatic["zoom_end"] - dramatic["zoom_start"])
        assert delta_dramatic > delta_subtle
        assert delta_subtle == pytest.approx(0.15 * 0.5, abs=1e-6)
        assert delta_dramatic == pytest.approx(0.15 * 1.5, abs=1e-6)

    def test_duration_maps_to_frame_count(self, engine: AnimationEngine) -> None:
        data = engine.get_zoompan_filter("ken_burns", 8.0, 30, "medium")["data"]
        assert data["total_frames"] == 240
        data = engine.get_zoompan_filter("ken_burns", 3.0, 24, "medium")["data"]
        assert data["total_frames"] == 72 and "d=72" in data["filter_string"]

    def test_default_resolution(self, engine: AnimationEngine) -> None:
        f = engine.get_zoompan_filter("ken_burns", 5.0, 30, "medium")["data"][
            "filter_string"
        ]
        assert "s=1920x1080" in f

    def test_all_presets_generate_nonempty_filters(
        self, engine: AnimationEngine
    ) -> None:
        catalog = engine.get_available_animations()["data"]["animations"]
        for anim in catalog:
            result = engine.get_zoompan_filter(anim["id"], 4.0, 30, "medium")
            assert result["success"] is True, anim["id"]
            assert result["data"]["filter_string"].startswith("zoompan="), anim["id"]


class TestValidation:
    """Clamping, defaults, warnings."""

    def test_unknown_type_defaults_with_warning(self, engine: AnimationEngine) -> None:
        result = engine.get_zoompan_filter("does_not_exist", 5.0, 30, "medium")
        assert result["data"]["animation_type"] == "ken_burns"
        assert any("Unknown animation" in w for w in result["warnings"])

    def test_invalid_duration_clamped(self, engine: AnimationEngine) -> None:
        result = engine.get_zoompan_filter("ken_burns", -5.0, 30, "medium")
        assert result["data"]["total_frames"] >= 1
        assert any("clamped" in w.lower() for w in result["warnings"])

    def test_invalid_intensity_and_fps(self, engine: AnimationEngine) -> None:
        result = engine.get_zoompan_filter("ken_burns", 5.0, 500, "wild")
        assert result["data"]["intensity"] == "medium"
        assert result["data"]["fps"] == 120
        assert len(result["warnings"]) >= 2


class TestSelection:
    """Weighted random and mood mapping."""

    def test_weighted_random_distribution(self, engine: AnimationEngine) -> None:
        allowed = {
            "slow_zoom_in",
            "slow_zoom_out",
            "ken_burns",
            "pan_left",
            "pan_right",
            "static",
        }
        picks = Counter(
            engine.select_random_documentary_animation()["data"]["animation_type"]
            for _ in range(2000)
        )
        assert set(picks) <= allowed and len(picks) >= 4
        # slow_zoom_in (30%) must be the mode; static (5%) must appear but be rare.
        assert picks.most_common(1)[0][0] == "slow_zoom_in"
        assert 0 < picks["static"] < 2000 * 0.15

    def test_mood_map(self, engine: AnimationEngine) -> None:
        cases = {
            "dramatic": "dramatic_zoom_in",
            "mysterious": "slow_zoom_in",
            "calm": "slow_zoom_out",
            "historical": "pan_left",
            "document": "vertical_scan",
            "memorial": "slow_zoom_out",
        }
        for mood, expected in cases.items():
            data = engine.get_animation_for_keyword_mood(mood)["data"]
            assert data["animation_type"] == expected, mood
        unknown = engine.get_animation_for_keyword_mood("xyzzy")["data"]
        assert unknown["animation_type"] == "ken_burns" and unknown["mapped"] is False


class TestBatchAndEasing:
    """Batch generation, easing helper, disable flag, integration smoke."""

    def _timeline(self) -> dict:
        return {
            "scenes": [
                {"id": "s1", "animation": "ken_burns", "duration": 8.0},
                {"id": "s2", "mood": "dramatic", "duration": 6.0},
                {"id": "s3", "duration": 4.0},  # no hint -> weighted random
            ]
        }

    def test_batch_produces_filter_per_scene(self, engine: AnimationEngine) -> None:
        result = engine.generate_batch_filters(
            self._timeline(), default_intensity="medium"
        )
        assert result["success"] is True
        filters = result["data"]["filters"]
        assert result["data"]["count"] == 3
        assert filters[0]["animation_type"] == "ken_burns"
        assert filters[1]["animation_type"] == "dramatic_zoom_in"
        assert all(f["filter_string"].startswith("zoompan=") for f in filters)

    def test_apply_easing_endpoints(self, engine: AnimationEngine) -> None:
        for mode in ("linear", "ease_in", "ease_out", "ease_in_out"):
            assert engine.apply_easing(0.0, mode) == pytest.approx(0.0)
            assert engine.apply_easing(1.0, mode) == pytest.approx(1.0)
        assert engine.apply_easing(0.5, "ease_in_out") == pytest.approx(0.5)
        assert engine.apply_easing(0.25, "ease_in") < 0.25
        assert engine.apply_easing(0.25, "ease_out") > 0.25

    def test_module_can_be_disabled(self, engine: AnimationEngine) -> None:
        assert engine.is_optional_module() is True
        engine.set_enabled(False)
        result = engine.get_zoompan_filter("ken_burns", 5.0, 30, "medium")
        assert result["success"] is False
        assert "disabled" in (result["error"] or "")
        engine.set_enabled(True)
        assert (
            engine.get_zoompan_filter("ken_burns", 5.0, 30, "medium")["success"] is True
        )

    def test_integration_smoke_fixture_script(
        self, engine: AnimationEngine, project_root: Path
    ) -> None:
        script_path = (
            project_root
            / "tests"
            / "fixtures"
            / "sample_project"
            / "script"
            / "sample_script.json"
        )
        script = json.loads(script_path.read_text(encoding="utf-8"))
        default_animation = script["project"]["default_animation"]
        scenes = [
            {
                "id": s["id"],
                "animation": default_animation,
                "duration": 8.0,
            }
            for s in script["scenes"]
        ]
        result = engine.generate_batch_filters({"scenes": scenes})
        assert result["success"] is True
        assert result["data"]["count"] == len(script["scenes"]) > 0
        for f in result["data"]["filters"]:
            assert re.search(r"zoompan=.+:d=\d+:s=1920x1080:fps=30", f["filter_string"])
            assert f["animation_type"] == "ken_burns"
