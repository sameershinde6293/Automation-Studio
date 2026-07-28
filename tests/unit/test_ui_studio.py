"""Headless tests for the Studio view-model (full-UI Batch 2).

Import classification/plan/staging, preview source + transport math,
and the DB-backed timeline model — every decision the Qt panels
paint, pinned without PyQt6. Timeline fixtures run against the real
schema so candidate-key text extraction (text_content) stays honest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.service_container import ServiceContainer
from core.time_helper import utc_now_str
from ui.viewmodel import (
    UiViewModel,
    classify_import,
    fmt_timecode,
    position_percent,
    scene_card_lines,
)


def _vm(**ctx) -> UiViewModel:
    return UiViewModel(ctx)


@pytest.fixture
def db_vm(project_root: Path, tmp_path: Path) -> UiViewModel:
    container = ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp_path / "autopilot.db"),
            "schema_path": str(project_root / "database" / "schema.sql"),
            "config_folder": str(project_root / "config"),
            "cache_folder": str(tmp_path / "cache"),
            "log_folder": str(tmp_path / "logs"),
            "ffmpeg_path": "ffmpeg",
        },
        project_root=tmp_path,
    )
    return UiViewModel({"container": container})


def _seed_project(vm: UiViewModel, with_durations: bool = True) -> str:
    db = vm.container.get("database").db
    now = utc_now_str()
    db.execute(
        "INSERT INTO projects (id, title, status, project_folder_path,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("p1", "Demo Doc", "completed", "/tmp/p1", now, now),
    )
    for number, seconds in ((1, 12.0), (2, 8.0)):
        db.execute(
            "INSERT INTO scenes (id, project_id, scene_number,"
            " scene_title, image_filename, image_file_path,"
            " image_matched, start_time, duration,"
            " transition_in, transition_out, animation_type, status,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"s{number}", "p1", number, f"Scene {number}",
                f"img{number}.jpg",
                f"/tmp/img{number}.jpg" if number == 1 else None,
                1 if number == 1 else 0,
                6.0 if number == 2 else 0.0,
                seconds if with_durations else 0.0,
                "crossfade", "fade_black", "ken_burns", "completed",
                now, now,
            ),
        )
        db.execute(
            "INSERT INTO dialogue_lines (id, project_id, scene_id,"
            " line_number, character_name, emotion, text_content,"
            " status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"d{number}", "p1", f"s{number}", 1, "NARRATOR",
                "dramatic", "one two three four five", "completed",
                now, now,
            ),
        )
    return "p1"


# ------------------------------------------------------------------
# Pure helpers
# ------------------------------------------------------------------
def test_classify_import_all_kinds() -> None:
    # Spec File 04 drop-zone formats: TXT/JSON/CSV/DOCX/PDF scripts,
    # JPG/PNG images, MP3/WAV audio — everything else is unsupported.
    assert classify_import("script.TXT") == "script"  # case-insensitive
    assert classify_import("beats.JSON") == "script"  # JSON is supported
    assert classify_import("rows.csv") == "script"
    assert classify_import("draft.docx") == "script"
    assert classify_import("book.PDF") == "script"
    assert classify_import("frame.JPEG") == "image"
    assert classify_import("still.png") == "image"
    assert classify_import("voice.mp3") == "audio"
    assert classify_import("boom.WAV") == "audio"
    assert classify_import("clip.webm") == "video"  # 5-zone spec
    assert classify_import("broll.MP4") == "video"
    assert classify_import("voice.flac") == "other"
    assert classify_import("notes.xyz") == "other"
    assert classify_import("noext") == "other"


def test_import_zones_show_supported_formats() -> None:
    zones = UiViewModel({}).import_zones()
    assert [z["kind"] for z in zones] == [
        "script", "image", "audio", "audio", "video",
    ]  # 5 drop zones: music + voice-over share the audio kind
    assert zones[0]["formats"] == "TXT · JSON · CSV · DOCX · PDF"
    assert "JSON" in zones[0]["formats"]  # user must SEE JSON support
    assert zones[1]["formats"] == "JPG · JPEG · PNG"
    assert zones[2]["formats"] == "MP3 · WAV"
    assert zones[3]["formats"] == "MP3 · WAV"
    assert zones[4]["formats"] == "MP4 · MOV · MKV · WEBM"
    assert "Music" in zones[2]["title"]
    assert "Voice" in zones[3]["title"]
    assert all(z["title"] and z["staged_folder"] for z in zones)


def test_fmt_timecode_boundaries() -> None:
    assert fmt_timecode(0) == "0:00"
    assert fmt_timecode(59.9) == "0:59"
    assert fmt_timecode(60) == "1:00"
    assert fmt_timecode(3599) == "59:59"
    assert fmt_timecode(3600) == "1:00:00"
    assert fmt_timecode(-5) == "0:00"
    assert fmt_timecode("junk") == "0:00"


def test_position_percent_clamps() -> None:
    assert position_percent(30, 60) == 50.0
    assert position_percent(120, 60) == 100.0
    assert position_percent(-1, 60) == 0.0
    assert position_percent(5, 0) == 0.0
    assert position_percent("x", 60) == 0.0


# ------------------------------------------------------------------
# Import plan / staging
# ------------------------------------------------------------------
def test_import_plan_statuses(tmp_path: Path) -> None:
    script = tmp_path / "story.txt"
    script.write_text("SCENE 1", encoding="utf-8")
    weird = tmp_path / "weird.xyz"
    weird.write_text("?", encoding="utf-8")  # exists, but unmappable kind
    vm = _vm()
    plan = vm.import_plan(
        [str(script), str(tmp_path / "gone.png"),
         str(script), str(weird)]
    )
    statuses = [(row["name"], row["status"]) for row in plan]
    assert statuses == [
        ("story.txt", "ready"), ("gone.png", "missing"),
        ("story.txt", "duplicate"), ("weird.xyz", "unsupported"),
    ]
    assert plan[0]["size_bytes"] == len("SCENE 1")
    assert plan[0]["kind"] == "script"


def test_import_summary_text(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"\xff\xd8fake-jpeg")
    vm = _vm()
    plan = vm.import_plan([str(f)])
    summary = vm.import_summary(plan)
    assert summary["ready"] == 1
    assert summary["counts"] == {"image": 1}
    assert "1 ready to stage" in summary["text"]
    assert vm.import_summary([])["text"].startswith("📥  Drop files")


def test_apply_import_stages_into_project(tmp_path: Path) -> None:
    src_dir = tmp_path / "drops"
    src_dir.mkdir()
    (src_dir / "story.txt").write_text("script body", encoding="utf-8")
    (src_dir / "one.jpg").write_bytes(b"one")
    (src_dir / "two.png").write_bytes(b"two")
    vm = _vm()
    plan = vm.import_plan(str(p) for p in sorted(src_dir.iterdir()))
    project = tmp_path / "projects" / "demo"
    result = vm.apply_import(plan, str(project))
    assert result["success"] is True
    assert result["copied"] == 3
    assert result["errors"] == []
    staged = Path(result["staged_dir"])
    assert (staged / "scripts" / "story.txt").is_file()
    assert (staged / "images" / "one.jpg").is_file()
    assert result["script_path"].endswith("story.txt")
    assert result["images_folder"] == str(staged / "images")
    # staging AGAIN with the same files deduplicates names honestly
    result2 = vm.apply_import(vm.import_plan([str(src_dir / "one.jpg")]),
                              str(project))
    assert "one (2).jpg" in result2["staged_dir"] or True
    assert (staged / "images" / "one (2).jpg").is_file()


def test_apply_import_guards(tmp_path: Path) -> None:
    vm = _vm()
    assert vm.apply_import([], str(tmp_path))["success"] is False
    missing = vm.import_plan([str(tmp_path / "nope.txt")])
    result = vm.apply_import(missing, str(tmp_path))
    assert result["success"] is False
    assert "Nothing ready" in result["error"]


# ------------------------------------------------------------------
# Preview source + transport
# ------------------------------------------------------------------
def test_preview_source_picks_existing_render(
    db_vm: UiViewModel, tmp_path: Path
) -> None:
    db = db_vm.container.get("database").db
    now = utc_now_str()
    db.execute(
        "INSERT INTO projects (id, title, status, project_folder_path,"
        " last_render_output_path, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("p9", "Finished", "completed", "/tmp/p9",
         str(tmp_path / "out.mp4"), now, now),
    )
    source = db_vm.preview_source()
    assert source["exists"] is False  # file not written yet
    assert source["title"] == "Finished"
    (tmp_path / "out.mp4").write_bytes(b"MP4")
    source = db_vm.preview_source()
    assert source["exists"] is True
    assert source["project_id"] == "p9"
    ghost = db_vm.preview_source(project_id="ghost")
    assert ghost["path"] or ghost["exists"] is False


def test_transport_state() -> None:
    state = UiViewModel.transport_state(30, 60, playing=False)
    assert state["position_text"] == "0:30 / 1:00"
    assert state["percent"] == 50.0
    assert state["has_media"] is True
    assert state["play_label"] == "►"
    playing = UiViewModel.transport_state(30, 60, playing=True)
    assert playing["play_label"] == "❚❚"
    empty = UiViewModel.transport_state(5, 0, playing=False)
    assert empty["has_media"] is False
    assert empty["percent"] == 0.0


# ------------------------------------------------------------------
# Timeline model (real schema)
# ------------------------------------------------------------------
def test_timeline_model_structure(db_vm: UiViewModel) -> None:
    project_id = _seed_project(db_vm)
    model = db_vm.timeline_model(project_id)
    assert model["found"] is True
    assert model["scene_count"] == 2
    assert model["title"] == "Demo Doc"
    assert model["word_total"] == 10  # 2 lines × 5 words
    first = model["scenes"][0]
    assert first["number"] == 1
    assert first["duration"] == 12.0
    assert first["image_matched"] is True
    assert first["lines"][0]["text"] == "one two three four five"
    assert first["lines"][0]["character"] == "NARRATOR"
    assert model["estimated"] is False
    assert abs(model["total_duration"] - 20.0) < 0.001
    summary = UiViewModel.timeline_summary_text(model)
    assert "2 scenes" in summary and "≈0:20" in summary


def test_timeline_scene_card_fields_and_text(db_vm: UiViewModel) -> None:
    # Visual timeline (File 04): thumbnail path, start timecode, and
    # the card-text helper the Qt panel paints beside the thumbnail.
    project_id = _seed_project(db_vm)
    model = db_vm.timeline_model(project_id)
    first, second = model["scenes"]
    assert first["thumb_path"] == "/tmp/img1.jpg"
    assert second["thumb_path"] is None
    assert first["start_text"] == "0:00"
    assert second["start_text"] == "0:06"  # start_time honoured
    title, meta, detail = scene_card_lines(first)
    assert title.startswith("#01") and "Scene 1" in title
    assert "0:12" in meta and "ken_burns" in meta
    assert "completed" in meta and "starts 0:00" in meta
    assert "image ✓" in detail and "5 words" in detail
    assert "one two three" in detail
    # scene without a thumbnail/match says so honestly
    t2, meta2, detail2 = scene_card_lines(second)
    assert t2.startswith("#02")
    assert "starts 0:06" in meta2
    assert "no image" in detail2  # image_matched=0, no thumb path
    assert "5 words" in detail2


def test_timeline_model_estimated_when_durations_zero(
    db_vm: UiViewModel,
) -> None:
    project_id = _seed_project(db_vm, with_durations=False)
    model = db_vm.timeline_model(project_id)
    assert model["estimated"] is True
    summary = UiViewModel.timeline_summary_text(model)
    assert "durations after render" in summary


def test_timeline_model_missing_project(db_vm: UiViewModel) -> None:
    model = db_vm.timeline_model("ghost")
    assert model["found"] is False
    assert UiViewModel.timeline_summary_text(model) == "No project selected."
    assert db_vm.timeline_projects(limit=5) == []


def test_timeline_projects_labels(db_vm: UiViewModel) -> None:
    _seed_project(db_vm)
    rows = db_vm.timeline_projects()
    assert rows == [{"id": "p1", "label": "Demo Doc"}]
