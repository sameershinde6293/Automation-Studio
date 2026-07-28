"""Unit tests for modules.timeline_engine.TimelineEngine."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core.service_container import ServiceContainer
from core.time_helper import utc_now_str
from modules.timeline_engine import TimelineEngine


@pytest.fixture
def container(project_root: Path, tmp_path: Path) -> ServiceContainer:
    """Isolated DI container."""
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
def engine(container: ServiceContainer) -> TimelineEngine:
    """Timeline engine fixture."""
    return TimelineEngine(container)


def _project(engine: TimelineEngine, title: str = "Timeline Test") -> str:
    pid = engine.db.new_id()
    assert engine.db.create_project(
        {
            "id": pid,
            "title": title,
            "project_folder_path": f"projects/{title.lower().replace(' ', '_')}",
        }
    )
    return pid


def _add_scene(
    engine: TimelineEngine,
    project_id: str,
    number: int,
    *,
    image: str = "img.jpg",
    mood: str = "neutral",
    transition_in: str = "crossfade",
    transition_out: str = "crossfade",
    transition_duration: float = 1.0,
    chapter_title: str = "",
    is_chapter: bool = False,
) -> str:
    sid = engine.db.new_id()
    engine.db.save_scene(
        {
            "id": sid,
            "project_id": project_id,
            "scene_number": number,
            "image_filename": image,
            "transition_in": transition_in,
            "transition_out": transition_out,
            "transition_duration": transition_duration,
        }
    )
    engine.db.db.execute(
        "UPDATE scenes SET keyword_mood = ?, chapter_title = ?, "
        "is_chapter_start = ?, transition_duration = ? WHERE id = ?",
        (mood, chapter_title, 1 if is_chapter else 0, transition_duration, sid),
    )
    return sid


def _add_line(
    engine: TimelineEngine,
    project_id: str,
    scene_id: str,
    line_number: int,
    text: str,
    duration: float,
    pause_after: str = "short",
) -> None:
    now = utc_now_str()
    engine.db.db.execute(
        "INSERT INTO dialogue_lines "
        "(id, project_id, scene_id, line_number, character_name, text_content, "
        "audio_duration, audio_generated, pause_after, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (
            engine.db.new_id(),
            project_id,
            scene_id,
            line_number,
            "NARRATOR",
            text,
            duration,
            pause_after,
            now,
            now,
        ),
    )


class TestBuildAndDurations:
    """Core timeline build tests."""

    def test_build_sample_like_ten_scenes(self, engine: TimelineEngine) -> None:
        pid = _project(engine)
        for i in range(1, 11):
            sid = _add_scene(
                engine,
                pid,
                i,
                image=f"scene_{i:02d}.jpg",
                mood="dramatic" if i < 4 else ("solemn" if i < 8 else "contemplative"),
            )
            _add_line(engine, pid, sid, 1, f"Line for scene {i}", 3.0)
            _add_line(engine, pid, sid, 2, f"Second line scene {i}", 2.0)
        result = engine.build_timeline(pid)
        assert result["success"] is True
        timeline = result["data"]["timeline"]
        assert len(timeline["scenes"]) == 10
        assert timeline["total_duration"] > 0
        # Each scene has timing
        for scene in timeline["scenes"]:
            assert scene["end_time"] > scene["start_time"]
            assert scene["duration"] > 0

    def test_scene_duration_from_lines(self, engine: TimelineEngine) -> None:
        pid = _project(engine, "Durations")
        sid = _add_scene(engine, pid, 1)
        _add_line(engine, pid, sid, 1, "One", 2.0, pause_after="short")
        _add_line(engine, pid, sid, 2, "Two", 3.0, pause_after="medium")
        _add_line(engine, pid, sid, 3, "Three", 1.5, pause_after="none")
        scenes = engine.db.db.fetch_all(
            "SELECT * FROM scenes WHERE project_id = ?", (pid,)
        )
        lines = engine._load_dialogue_by_scene(pid)
        timed = engine.calculate_scene_durations(scenes, lines)
        # 2+3+1.5 + short0.4 + medium0.85 + entry/exit padding 0.3 = 8.05
        assert timed[0]["duration"] == pytest.approx(8.05, abs=0.05)

    def test_align_scenes_to_audio(self, engine: TimelineEngine) -> None:
        scenes = [
            {"id": "a", "duration": 10.0, "type": "scene"},
            {"id": "b", "duration": 10.0, "type": "scene"},
        ]
        aligned = engine.align_scenes_to_audio(scenes, 30.0)
        total = sum(float(s["duration"]) for s in aligned)
        assert total == pytest.approx(30.0, abs=0.02)

    def test_transition_overlap_crossfade(self, engine: TimelineEngine) -> None:
        result = engine.calculate_transition_overlap(
            {"transition_out": "crossfade"},
            {"transition_in": "crossfade"},
        )
        assert result["success"] is True
        assert result["data"]["overlap"] == 1.0

    def test_transition_overlap_hard_cut(self, engine: TimelineEngine) -> None:
        result = engine.calculate_transition_overlap(
            {"transition_out": "hard_cut"},
            {"transition_in": "hard_cut"},
        )
        assert result["data"]["overlap"] == 0.0

    def test_crossfade_timing_overlap(self, engine: TimelineEngine) -> None:
        pid = _project(engine, "Overlap")
        s1 = _add_scene(
            engine, pid, 1, transition_out="crossfade", transition_duration=1.0
        )
        s2 = _add_scene(engine, pid, 2, transition_in="crossfade")
        _add_line(engine, pid, s1, 1, "A", 5.0)
        _add_line(engine, pid, s2, 1, "B", 5.0)
        timeline = engine.build_timeline(pid)["data"]["timeline"]
        a, b = timeline["scenes"]
        # B starts near A.end - 1s
        assert b["start_time"] == pytest.approx(a["end_time"] - 1.0, abs=0.05)


class TestChaptersIntroOutro:
    """Chapters and intro/outro."""

    def test_chapter_markers_by_mood(self, engine: TimelineEngine) -> None:
        pid = _project(engine, "Chapters")
        # Long enough scenes so chapters exceed min length after build
        for i, mood in enumerate(
            ["dramatic"] * 3 + ["solemn"] * 3 + ["contemplative"] * 3, start=1
        ):
            sid = _add_scene(engine, pid, i, mood=mood, image=f"{mood}_{i}.jpg")
            _add_line(engine, pid, sid, 1, "x " * 40, 12.0)
        timeline = engine.build_timeline(pid)["data"]["timeline"]
        chapters = timeline["chapters"]
        assert len(chapters) >= 2
        assert chapters[0]["time"] == 0.0 or chapters[0]["time"] >= 0.0
        text = timeline["youtube_chapters_text"]
        assert ":" in text.split()[0]
        assert "\n" in text or len(chapters) == 1

    def test_youtube_format(self, engine: TimelineEngine) -> None:
        text = engine.export_youtube_chapters_text(
            [
                {"time": 0, "title": "Introduction"},
                {"time": 125, "title": "The Setup"},
                {"time": 3725, "title": "Discovery"},
            ]
        )
        lines = text.splitlines()
        assert lines[0] == "0:00 Introduction"
        assert lines[1] == "2:05 The Setup"
        assert lines[2] == "1:02:05 Discovery"

    def test_intro_shifts_scenes(self, engine: TimelineEngine) -> None:
        pid = _project(engine, "Intro")
        sid = _add_scene(engine, pid, 1)
        _add_line(engine, pid, sid, 1, "Hello", 4.0)
        base = engine.build_timeline(pid, intro_config={"enabled": False})["data"][
            "timeline"
        ]
        with_intro = engine.build_timeline(
            pid, intro_config={"enabled": True, "duration": 5.0}
        )["data"]["timeline"]
        assert with_intro["intro"]["duration"] == 5.0
        assert with_intro["scenes"][0]["start_time"] == pytest.approx(
            base["scenes"][0]["start_time"] + 5.0, abs=0.05
        )

    def test_outro_appended(self, engine: TimelineEngine) -> None:
        pid = _project(engine, "Outro")
        sid = _add_scene(engine, pid, 1)
        _add_line(engine, pid, sid, 1, "Bye", 3.0)
        timeline = engine.build_timeline(
            pid, outro_config={"enabled": True, "duration": 5.0}
        )["data"]["timeline"]
        assert timeline["outro"] is not None
        assert timeline["outro"]["duration"] == 5.0
        assert timeline["total_duration"] >= timeline["scenes"][-1]["end_time"]


class TestValidationAndQueries:
    """Validation and lookup helpers."""

    def test_validation_detects_gap(self, engine: TimelineEngine) -> None:
        timeline = {
            "scenes": [
                {
                    "id": "1",
                    "scene_number": 1,
                    "start_time": 0.0,
                    "end_time": 5.0,
                    "duration": 5.0,
                    "transition_out": "hard_cut",
                    "type": "scene",
                },
                {
                    "id": "2",
                    "scene_number": 2,
                    "start_time": 8.0,
                    "end_time": 12.0,
                    "duration": 4.0,
                    "transition_in": "hard_cut",
                    "type": "scene",
                },
            ],
            "all_items": [],
            "narration_duration": 9.0,
            "content_duration": 9.0,
        }
        timeline["all_items"] = timeline["scenes"]
        result = engine.validate_timeline(timeline)
        assert result["success"] is True
        assert result["data"]["valid"] is False
        assert any("Gap" in issue for issue in result["data"]["issues"])

    def test_get_scene_at_time(self, engine: TimelineEngine) -> None:
        pid = _project(engine, "Seek")
        s1 = _add_scene(engine, pid, 1)
        s2 = _add_scene(engine, pid, 2)
        _add_line(engine, pid, s1, 1, "A", 10.0)
        _add_line(engine, pid, s2, 1, "B", 10.0)
        engine.build_timeline(pid)
        # After overlap, time 5 should still be scene 1-ish; 12 near scene 2
        hit = engine.get_scene_at_time(pid, 0.5)
        assert hit["success"] is True
        assert hit["data"]["scene"] is not None
        assert hit["data"]["scene"]["scene_number"] == 1

    def test_scene_boundaries(self, engine: TimelineEngine) -> None:
        pid = _project(engine, "Bounds")
        for i in range(1, 11):
            sid = _add_scene(engine, pid, i)
            _add_line(engine, pid, sid, 1, "x", 2.0)
        result = engine.get_scene_boundaries(pid)
        assert result["success"] is True
        assert result["data"]["count"] == 10
        bounds = result["data"]["boundaries"]
        assert bounds[0]["start"] <= bounds[0]["end"]
        assert bounds[-1]["end"] >= bounds[0]["end"]

    def test_empty_project(self, engine: TimelineEngine) -> None:
        pid = _project(engine, "Empty")
        result = engine.build_timeline(pid)
        assert result["success"] is True
        assert result["data"]["timeline"]["scenes"] == []
        assert result["data"].get("empty") is True

    def test_single_scene(self, engine: TimelineEngine) -> None:
        pid = _project(engine, "Single")
        sid = _add_scene(engine, pid, 1, transition_out="hard_cut")
        _add_line(engine, pid, sid, 1, "Only one", 6.0)
        timeline = engine.build_timeline(pid)["data"]["timeline"]
        assert len(timeline["scenes"]) == 1
        assert timeline["scenes"][0]["start_time"] == 0.0
        assert timeline["total_duration"] == pytest.approx(
            timeline["scenes"][0]["duration"], abs=0.05
        )

    def test_save_and_reload(self, engine: TimelineEngine) -> None:
        pid = _project(engine, "Persist")
        sid = _add_scene(engine, pid, 1)
        _add_line(engine, pid, sid, 1, "Persist me", 4.0)
        built = engine.build_timeline(pid, save=True)["data"]["timeline"]
        row = engine.db.db.fetch_one(
            "SELECT timeline_json, youtube_chapters_text FROM timeline_data "
            "WHERE project_id = ?",
            (pid,),
        )
        assert row is not None
        loaded = json.loads(row["timeline_json"])
        assert loaded["total_duration"] == built["total_duration"]
        assert len(loaded["scenes"]) == 1
        assert row["youtube_chapters_text"]

    def test_long_project_performance(self, engine: TimelineEngine) -> None:
        pid = _project(engine, "Long")
        for i in range(1, 101):
            sid = _add_scene(
                engine, pid, i, mood="dramatic" if i % 2 == 0 else "solemn"
            )
            _add_line(engine, pid, sid, 1, "word " * 5, 2.0)
        t0 = time.perf_counter()
        result = engine.build_timeline(pid)
        elapsed = time.perf_counter() - t0
        assert result["success"] is True
        assert len(result["data"]["timeline"]["scenes"]) == 100
        assert elapsed < 5.0
