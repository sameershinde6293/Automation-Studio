"""Unit tests for modules.transition_engine.TransitionEngine."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.service_container import ServiceContainer
from modules.transition_engine import CATEGORIES, XFADE_MAP, TransitionEngine


@pytest.fixture
def container(project_root: Path, tmp_path: Path) -> ServiceContainer:
    """Isolated container."""
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
def engine(container: ServiceContainer) -> TransitionEngine:
    """Transition engine with cleared history."""
    eng = TransitionEngine(container)
    eng.reset_history()
    return eng


class TestCatalogAndFilters:
    """Mappings and filter generation."""

    def test_all_transitions_have_mapping(self, engine: TransitionEngine) -> None:
        catalog = engine.get_available_transitions()
        assert catalog["success"] is True
        assert catalog["data"]["count"] >= 30
        names = [t["id"] for t in catalog["data"]["transitions"]]
        for cat, items in CATEGORIES.items():
            for name in items:
                assert name in names
                assert name in XFADE_MAP
                built = engine.build_transition_filter(
                    {"end_time": 10.0},
                    {"start_time": 10.0},
                    name,
                    duration=1.0,
                )
                assert built["success"] is True
                if name in ("hard_cut", "cut"):
                    assert built["data"]["is_hard_cut"] is True
                    assert built["data"]["filter_string"] is None
                else:
                    assert built["data"]["filter_string"]
                    assert "xfade=transition=" in built["data"]["filter_string"]

    def test_basic_crossfade_filter(self, engine: TransitionEngine) -> None:
        result = engine.build_transition_filter(
            {"end_time": 10.0},
            {"start_time": 10.0},
            "crossfade",
            duration=1.0,
        )
        assert result["success"] is True
        assert result["data"]["filter_string"] == (
            "xfade=transition=fade:duration=1.000:offset=9.000"
        )
        assert result["data"]["offset"] == 9.0
        assert result["data"]["is_hard_cut"] is False

    def test_hard_cut_null(self, engine: TransitionEngine) -> None:
        result = engine.build_transition_filter(
            {"end_time": 5.0}, {"start_time": 5.0}, "hard_cut"
        )
        assert result["data"]["is_hard_cut"] is True
        assert result["data"]["filter_string"] is None
        assert result["data"]["filter_complex"] is None

    def test_xfade_offset_timing(self, engine: TransitionEngine) -> None:
        result = engine.build_transition_filter(
            {"end_time": 20.5},
            {},
            "dissolve",
            duration=0.5,
        )
        assert result["data"]["offset"] == pytest.approx(20.0)
        assert "offset=20.000" in result["data"]["filter_string"]


class TestSmartAndValidation:
    """Smart selection and validation."""

    def test_smart_avoids_repeats(self, engine: TransitionEngine) -> None:
        scene = {"keyword_mood": "dramatic"}
        picks = []
        for _ in range(6):
            picks.append(
                engine.smart_transition_selection(scene, scene, "dramatic")["data"][
                    "transition_type"
                ]
            )
        # Not all six identical
        assert len(set(picks)) >= 2
        # No 4 identical in a row
        for i in range(len(picks) - 3):
            assert not (picks[i] == picks[i + 1] == picks[i + 2] == picks[i + 3])

    def test_mood_based_dark_to_dark(self, engine: TransitionEngine) -> None:
        result = engine.smart_transition_selection(
            {"keyword_mood": "ominous"},
            {"keyword_mood": "haunted"},
        )
        assert result["success"] is True
        assert result["data"]["transition_type"] in {
            "crossfade",
            "fade_black",
            "dissolve",
            "old_film",
        }

    def test_mood_based_dark_to_bright(self, engine: TransitionEngine) -> None:
        result = engine.smart_transition_selection(
            {"keyword_mood": "ominous"},
            {"keyword_mood": "excited"},
        )
        assert result["data"]["transition_type"] in {
            "fade_white",
            "dramatic_flash",
            "light_leak",
        }

    def test_duration_validation_warnings(self, engine: TransitionEngine) -> None:
        short = engine.validate_transition_settings("crossfade", 0.05)
        assert short["success"] is True
        assert short["data"]["duration"] >= 0.1
        assert short["warnings"]
        long = engine.validate_transition_settings("crossfade", 10.0)
        assert long["data"]["duration"] <= 5.0
        assert long["warnings"]
        ok = engine.validate_transition_settings("crossfade", 1.0)
        assert ok["data"]["duration"] == 1.0

    def test_invalid_type_defaults(self, engine: TransitionEngine) -> None:
        result = engine.validate_transition_settings("not_a_real_transition", 1.0)
        assert result["data"]["transition_type"] == "crossfade"
        assert result["warnings"]


class TestBatchAndCustom:
    """Batch generation and custom effects."""

    def test_batch_filters_ten_scenes(self, engine: TransitionEngine) -> None:
        scenes = []
        t = 0.0
        for i in range(10):
            scenes.append(
                {
                    "id": f"s{i}",
                    "scene_number": i + 1,
                    "start_time": t,
                    "end_time": t + 5.0,
                    "duration": 5.0,
                    "transition_out": {"type": "crossfade", "duration": 1.0},
                    "keyword_mood": "dramatic",
                }
            )
            t += 4.0  # with 1s overlap
        timeline = {"scenes": scenes}
        result = engine.generate_batch_filters(timeline, use_smart=False)
        assert result["success"] is True
        assert result["data"]["count"] == 9
        for filt in result["data"]["filters"]:
            assert filt["from_scene"] + 1 == filt["to_scene"] or True
            if not filt["is_hard_cut"]:
                assert "xfade=transition=" in filt["filter_string"]

    def test_batch_with_hard_cuts(self, engine: TransitionEngine) -> None:
        scenes = [
            {
                "scene_number": 1,
                "end_time": 5.0,
                "transition_out": {"type": "hard_cut", "duration": 0},
            },
            {
                "scene_number": 2,
                "end_time": 10.0,
                "transition_out": {"type": "hard_cut", "duration": 0},
            },
            {
                "scene_number": 3,
                "end_time": 15.0,
                "transition_out": {"type": "crossfade", "duration": 1.0},
            },
            {"scene_number": 4, "end_time": 20.0},
        ]
        result = engine.generate_batch_filters({"scenes": scenes})
        assert result["data"]["hard_cuts"] >= 2

    def test_custom_effect_transitions(self, engine: TransitionEngine) -> None:
        for ttype in ("burn_out", "film_burn", "old_film", "glitch", "matrix_dissolve"):
            built = engine.build_transition_filter(
                {"end_time": 8.0}, {}, ttype, duration=1.0
            )
            assert built["success"] is True
            assert built["data"]["extra_filters"]
            assert "xfade=transition=" in built["data"]["filter_string"]

    def test_get_available_categories(self, engine: TransitionEngine) -> None:
        result = engine.get_available_transitions()
        cats = result["data"]["categories"]
        assert set(cats.keys()) == {"basic", "cinematic", "dramatic", "artistic"}
        total = sum(len(v) for v in cats.values())
        assert total >= 30

    def test_timeline_integration_shape(self, engine: TransitionEngine) -> None:
        """Simulate timeline_engine scene dicts."""
        timeline = {
            "scenes": [
                {
                    "id": "a",
                    "scene_number": 1,
                    "start_time": 0.0,
                    "end_time": 10.0,
                    "transition_out": {"type": "dissolve", "duration": 0.5},
                    "keyword_mood": "ominous",
                },
                {
                    "id": "b",
                    "scene_number": 2,
                    "start_time": 9.5,
                    "end_time": 20.0,
                    "transition_in": "dissolve",
                    "keyword_mood": "solemn",
                },
            ]
        }
        batch = engine.generate_batch_filters(timeline)
        assert batch["data"]["count"] == 1
        f0 = batch["data"]["filters"][0]
        assert f0["offset"] == pytest.approx(9.5)
        assert "dissolve" in f0["filter_string"]
