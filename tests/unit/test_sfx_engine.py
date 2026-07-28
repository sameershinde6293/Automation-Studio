"""Unit tests for modules.sfx_engine.SFXEngine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.service_container import ServiceContainer
from core.time_helper import utc_now_str
from modules.sfx_engine import SFXEngine


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
def sfx(container: ServiceContainer) -> SFXEngine:
    """SFXEngine instance."""
    return SFXEngine(container)


@pytest.fixture
def project_id(sfx: SFXEngine) -> str:
    """Create empty project."""
    pid = sfx.db.new_id()
    assert sfx.db.create_project(
        {
            "id": pid,
            "title": "SFX Test",
            "project_folder_path": "projects/sfx_test",
        }
    )
    return pid


def _add_scene_with_text(
    sfx: SFXEngine,
    project_id: str,
    text: str,
    scene_number: int = 1,
    start_time: float = 0.0,
    word_times: list[dict] | None = None,
) -> str:
    """Insert scene + dialogue (+ optional word timestamps JSON)."""
    scene_id = sfx.db.new_id()
    sfx.db.save_scene(
        {
            "id": scene_id,
            "project_id": project_id,
            "scene_number": scene_number,
            "image_filename": "img.jpg",
            "start_time": start_time,
            "end_time": start_time + 10.0,
            "duration": 10.0,
        }
    )
    # fix start_time if save_scene doesn't set it
    sfx.db.db.execute(
        "UPDATE scenes SET start_time = ?, end_time = ?, duration = ? WHERE id = ?",
        (start_time, start_time + 10.0, 10.0, scene_id),
    )
    line_id = sfx.db.new_id()
    now = utc_now_str()
    ts_json = json.dumps(word_times) if word_times is not None else None
    sfx.db.db.execute(
        "INSERT INTO dialogue_lines "
        "(id, project_id, scene_id, line_number, character_name, text_content, "
        "word_timestamps_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            line_id,
            project_id,
            scene_id,
            1,
            "NARRATOR",
            text,
            ts_json,
            now,
            now,
        ),
    )
    return scene_id


class TestLibrary:
    """Library loading."""

    def test_load_sfx_library(self, sfx: SFXEngine) -> None:
        result = sfx.load_sfx_library()
        assert result["success"] is True
        assert result["data"]["count"] >= 10
        assert result["data"]["present"] >= 1
        assert "categories" in result["data"]
        # church_bell should exist after placeholder generation
        entries = result["data"]["catalog"]["entries"]
        assert "church_bell" in entries
        assert entries["church_bell"]["exists"] is True


class TestAutoPlacement:
    """Keyword auto placement."""

    def test_death_keyword_places_church_bell(
        self, sfx: SFXEngine, project_id: str
    ) -> None:
        words = [
            {"word": "The", "start": 0.0, "end": 0.2},
            {"word": "king", "start": 0.2, "end": 0.5},
            {"word": "died", "start": 1.2, "end": 1.5},
            {"word": "in", "start": 1.5, "end": 1.7},
            {"word": "his", "start": 1.7, "end": 1.9},
            {"word": "sleep", "start": 1.9, "end": 2.3},
        ]
        _add_scene_with_text(
            sfx,
            project_id,
            "The king died in his sleep",
            word_times=words,
        )
        result = sfx.auto_place_sfx(project_id)
        assert result["success"] is True
        names = [p["sfx_name"] for p in result["data"]["placements"]]
        assert "church_bell" in names
        bell = next(
            p for p in result["data"]["placements"] if p["sfx_name"] == "church_bell"
        )
        # near "died" at 1.2 with small offset
        assert abs(float(bell["timestamp_seconds"]) - 1.15) < 0.15

    def test_war_scene_places_battlefield(
        self, sfx: SFXEngine, project_id: str
    ) -> None:
        _add_scene_with_text(
            sfx,
            project_id,
            "The armies clashed on the battlefield after the war.",
        )
        result = sfx.auto_place_sfx(project_id)
        assert result["success"] is True
        names = {p["sfx_name"] for p in result["data"]["placements"]}
        assert "battlefield" in names

    def test_multiple_sfx_per_scene(self, sfx: SFXEngine, project_id: str) -> None:
        _add_scene_with_text(
            sfx,
            project_id,
            "A shocking death in the war left the church in fear and panic.",
        )
        result = sfx.auto_place_sfx(project_id)
        assert result["success"] is True
        assert result["data"]["count"] >= 2


class TestManualCrud:
    """Manual placement and CRUD."""

    def test_manual_placement(self, sfx: SFXEngine, project_id: str) -> None:
        sfx.load_sfx_library()
        scene_id = _add_scene_with_text(sfx, project_id, "Hello world")
        result = sfx.place_sfx_manually(
            project_id, scene_id, "dramatic_boom", 3.5, volume=0.75
        )
        assert result["success"] is True
        assert result["data"]["placement"]["sfx_name"] == "dramatic_boom"
        assert float(result["data"]["placement"]["timestamp_seconds"]) == pytest.approx(
            3.5
        )
        assert float(result["data"]["placement"]["volume"]) == pytest.approx(0.75)

    def test_get_all_ordered(self, sfx: SFXEngine, project_id: str) -> None:
        sfx.load_sfx_library()
        sfx.place_sfx_manually(project_id, None, "dramatic_boom", 5.0)
        sfx.place_sfx_manually(project_id, None, "church_bell", 1.0)
        sfx.place_sfx_manually(project_id, None, "clock_ticking", 3.0)
        result = sfx.get_all_placements(project_id)
        assert result["success"] is True
        stamps = [float(p["timestamp_seconds"]) for p in result["data"]["placements"]]
        assert stamps == sorted(stamps)
        assert len(stamps) == 3

    def test_remove_placement(self, sfx: SFXEngine, project_id: str) -> None:
        sfx.load_sfx_library()
        placed = sfx.place_sfx_manually(project_id, None, "dramatic_boom", 2.0)
        pid = placed["data"]["placement"]["id"]
        removed = sfx.remove_placement(pid)
        assert removed["success"] is True
        assert removed["data"]["deleted"] is True
        allp = sfx.get_all_placements(project_id)["data"]["placements"]
        assert all(p["id"] != pid for p in allp)

    def test_update_placement(self, sfx: SFXEngine, project_id: str) -> None:
        sfx.load_sfx_library()
        placed = sfx.place_sfx_manually(project_id, None, "church_bell", 2.0)
        pid = placed["data"]["placement"]["id"]
        updated = sfx.update_placement(pid, {"timestamp": 4.5, "volume": 0.55})
        assert updated["success"] is True
        row = updated["data"]["placement"]
        assert float(row["timestamp_seconds"]) == pytest.approx(4.5)
        assert float(row["volume"]) == pytest.approx(0.55)


class TestSuggestPrepareIntegration:
    """Suggestions, prepare for mix, analyzer, missing file, disable."""

    def test_suggestions(self, sfx: SFXEngine, project_id: str) -> None:
        scene_id = _add_scene_with_text(
            sfx,
            project_id,
            "Death in war brought fear and a shocking revelation.",
        )
        result = sfx.suggest_sfx_for_scene(project_id, scene_id)
        assert result["success"] is True
        suggestions = result["data"]["suggestions"]
        assert 1 <= len(suggestions) <= 5
        assert all("score" in s for s in suggestions)
        assert suggestions[0]["score"] >= suggestions[-1]["score"]

    def test_prepare_for_mixing(self, sfx: SFXEngine, project_id: str) -> None:
        sfx.load_sfx_library()
        sfx.place_sfx_manually(project_id, None, "dramatic_boom", 1.25)
        result = sfx.prepare_sfx_for_mixing(project_id)
        assert result["success"] is True
        assert result["data"]["count"] == 1
        item = result["data"]["sfx_list"][0]
        for key in ("path", "timestamp", "volume", "fade_in", "fade_out"):
            assert key in item
        assert Path(item["path"]).exists()
        assert float(item["timestamp"]) == pytest.approx(1.25)

    def test_keyword_analyzer_integration(
        self, sfx: SFXEngine, project_id: str
    ) -> None:
        # Text that analyzer maps to SFX via its own map
        _add_scene_with_text(
            sfx,
            project_id,
            "The death toll and war left the crowd in fear.",
        )
        result = sfx.auto_place_sfx(project_id)
        assert result["success"] is True
        assert result["data"]["count"] >= 1

    def test_missing_sfx_file_graceful(
        self, sfx: SFXEngine, project_id: str, tmp_path: Path
    ) -> None:
        sfx.load_sfx_library()
        # Point a catalog entry at missing path
        sfx._catalog["entries"]["ghost_missing"] = {
            "name": "ghost_missing",
            "path": str(tmp_path / "nope.wav"),
            "relative_path": "nope.wav",
            "category": "horror",
            "exists": False,
            "volume": 0.5,
            "fade_in": 0.1,
            "fade_out": 0.3,
        }
        result = sfx.place_sfx_manually(project_id, None, "ghost_missing", 1.0)
        assert result["success"] is False
        assert "missing" in (result["error"] or "").lower()

    def test_module_disabled_skips_auto(self, sfx: SFXEngine, project_id: str) -> None:
        _add_scene_with_text(sfx, project_id, "The king died suddenly.")
        sfx.set_enabled(False)
        auto = sfx.auto_place_sfx(project_id)
        assert auto["success"] is True
        assert auto["data"]["count"] == 0
        assert auto["data"].get("disabled") is True
        # Manual still works when re-enabled for save path; require enabled for catalog
        sfx.set_enabled(True)
        sfx.load_sfx_library()
        manual = sfx.place_sfx_manually(project_id, None, "dramatic_boom", 0.5)
        assert manual["success"] is True

    def test_fade_params_present(self, sfx: SFXEngine, project_id: str) -> None:
        sfx.load_sfx_library()
        placed = sfx.place_sfx_manually(project_id, None, "ominous_drone", 0.0)
        p = placed["data"]["placement"]
        assert float(p["fade_in"]) >= 0.0
        assert float(p["fade_out"]) >= 0.0
