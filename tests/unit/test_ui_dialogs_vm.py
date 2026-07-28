"""Headless tests for the Batch 3 dialog view-models.

New-project defaults/validation, crash-recovery candidates (seeded
render_progress rows on the real schema), discard, the render-complete
model, and the one-off Drive upload seam — all without PyQt6.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.service_container import ServiceContainer
from core.time_helper import utc_now_str
from ui.viewmodel import UiViewModel, slugify_title


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


def _seed_recovery(vm: UiViewModel) -> None:
    db = vm.container.get("database").db
    now = utc_now_str()
    db.execute(
        "INSERT INTO projects (id, title, status, project_folder_path,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("p-int", "Interrupted Doc", "rendering", "/tmp/p", now, now),
    )
    db.execute(
        "INSERT INTO projects (id, title, status, project_folder_path,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("p-done", "Finished Doc", "completed", "/tmp/p2", now, now),
    )
    db.execute(
        "INSERT INTO render_progress (id, project_id, render_session_id,"
        " current_stage, stage_percent, total_scenes, error_count,"
        " is_resumable, started_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "p-int", "sess-1", "tts_generating", 42.5, 4, 1, 1,
         now, now),
    )
    db.execute(
        "INSERT INTO render_progress (id, project_id, render_session_id,"
        " current_stage, stage_percent, total_scenes, error_count,"
        " is_resumable, started_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("r2", "p-done", "sess-2", "completed", 100.0, 3, 0, 1,
         now, now),
    )


# ------------------------------------------------------------------
# New project
# ------------------------------------------------------------------
def test_slugify_title_rules() -> None:
    assert slugify_title("The Great  Fire of London!") == (
        "the-great-fire-of-london"
    )
    assert slugify_title("") == "untitled"
    assert slugify_title("---") == "untitled"
    assert slugify_title("Déjà Vu") == "déjà-vu"  # unicode kept (NTFS-safe)
    assert slugify_title("मृत्यूंचा इतिहास") != "untitled"  # Indic titles


def test_new_project_defaults_and_validation(tmp_path: Path) -> None:
    vm = _vm()
    defaults = vm.new_project_defaults("My Documentary")
    assert defaults["folder"] == "projects/my-documentary"
    ok, message = vm.validate_new_project("", str(tmp_path))
    assert ok is False and "title" in message
    ok, message = vm.validate_new_project("Doc", "")
    assert ok is False and "folder" in message
    ok, message = vm.validate_new_project("Doc", str(tmp_path / "new"))
    assert ok is True and message == ""
    assert (tmp_path / "new").is_dir()  # validation creates it honestly


# ------------------------------------------------------------------
# Recovery
# ------------------------------------------------------------------
def test_recovery_candidates_filtering(db_vm: UiViewModel) -> None:
    _seed_recovery(db_vm)
    candidates = db_vm.recovery_candidates()
    assert len(candidates) == 1  # completed + non-resumable excluded
    candidate = candidates[0]
    assert candidate["project_id"] == "p-int"
    assert candidate["title"] == "Interrupted Doc"
    assert candidate["stage"] == "tts_generating"
    assert candidate["percent"] == 42.5
    assert candidate["error_count"] == 1
    assert _vm().recovery_candidates() == []  # no container -> []


def test_discard_recovery(db_vm: UiViewModel) -> None:
    _seed_recovery(db_vm)
    ok, _message = db_vm.discard_recovery("p-int")
    assert ok is True
    assert db_vm.recovery_candidates() == []
    ok, _message = db_vm.discard_recovery("ghost")  # idempotent
    assert ok is True
    assert _vm().discard_recovery("x")[0] is False  # no container


# ------------------------------------------------------------------
# Render complete
# ------------------------------------------------------------------
def test_render_complete_model(db_vm: UiViewModel,
                               tmp_path: Path) -> None:
    output = tmp_path / "final.mp4"
    output.write_bytes(b"MP4DATA" * 200)  # 1400 bytes
    result = {"data": {"output_file_path": str(output)},
              "warnings": ["w1"]}
    model = db_vm.render_complete_model(result)
    assert model["exists"] is True
    assert model["size_text"] == "1.4 KB"
    assert model["drive_ready"] is False  # engine absent here
    assert model["warnings"] == ["w1"]
    empty = db_vm.render_complete_model({"data": {}})
    assert empty["exists"] is False
    assert empty["size_text"] == ""
    bare = _vm().render_complete_model({"data": {}})
    assert bare["chapters"] == [] and bare["chapters_text"] == ""
    assert bare["thumbnail_path"] is None and bare["duration_text"] == ""


def _seed_render_outputs(vm: UiViewModel, tmp_path: Path) -> str:
    db = vm.container.get("database").db
    now = utc_now_str()
    output = tmp_path / "final.mp4"
    output.write_bytes(b"MP4" * 100)
    thumb = tmp_path / "thumb1.jpg"
    thumb.write_bytes(b"\xff\xd8thumb-bytes")
    db.execute(
        "INSERT INTO projects (id, title, status, project_folder_path,"
        " last_render_output_path, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("p9", "Doc Nine", "completed", str(tmp_path), str(output),
         now, now),
    )
    for number, start, chapter in ((1, 0.0, ""), (2, 12.0, "The Fall")):
        db.execute(
            "INSERT INTO scenes (id, project_id, scene_number,"
            " scene_title, chapter_title, start_time, duration,"
            " status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"s{number}", "p9", number, f"Scene {number}", chapter,
             start, 12.0, "completed", now, now),
        )
    db.execute(
        "INSERT INTO thumbnails (id, project_id, variation_number,"
        " file_path, is_selected, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("t1", "p9", 1, str(thumb), 1, now),
    )
    db.execute(
        "INSERT INTO render_history (id, project_id, render_session_id,"
        " started_at, completed_at, video_duration_seconds,"
        " output_file_path, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("h1", "p9", "sess-9", now, now, 24.0, str(output), "completed"),
    )
    return str(output)


def test_render_complete_enrichment(db_vm: UiViewModel,
                                    tmp_path: Path) -> None:
    # File 04 dialog facts: thumbnail preview, duration, YouTube
    # chapters — all pulled from the DB by output path alone.
    output = _seed_render_outputs(db_vm, tmp_path)
    model = db_vm.render_complete_model(
        {"data": {"output_file_path": output}}
    )
    assert model["project_id"] == "p9"
    assert model["duration_text"] == "0:24"
    assert model["thumbnail_path"].endswith("thumb1.jpg")
    assert model["chapters"][0]["time"] == "0:00"  # YouTube rule
    assert model["chapters"][1] == {
        "time": "0:12", "seconds": 12.0, "title": "The Fall",
    }
    assert model["chapters_text"] == "0:00 Scene 1\n0:12 The Fall"


# ------------------------------------------------------------------
# Pre-render report (File 04: quality checks before the render)
# ------------------------------------------------------------------
def test_pre_render_report_ready_path(tmp_path: Path) -> None:
    script = tmp_path / "doc.txt"
    script.write_text("one two three four five six", encoding="utf-8")
    images = tmp_path / "imgs"
    images.mkdir()
    (images / "a.jpg").write_bytes(b"x")
    (images / "b.png").write_bytes(b"x")
    vm = _vm()
    model = vm.pre_render_report_model(
        str(script), str(images), str(tmp_path / "proj"), "My Doc"
    )
    assert model["ready"] is True
    assert model["errors"] == 0
    assert model["words"] == 6
    assert "≈0:02" in model["estimate_text"]  # 6 words at 150 wpm
    assert "Ready to render" in model["summary_text"]
    rows = {row["label"]: row for row in model["rows"]}
    assert rows["Script format"]["value"] == "TXT"
    assert rows["Images"]["value"].startswith("2 JPG/PNG")
    # no engines on a bare view-model -> honest warnings, not crashes
    assert rows["FFmpeg"]["level"] == "warn"
    assert rows["Piper TTS"]["level"] == "warn"
    assert rows["License"]["level"] == "info"


def test_pre_render_report_blocks_on_errors(tmp_path: Path) -> None:
    vm = _vm()
    bad = vm.pre_render_report_model(
        str(tmp_path / "gone.txt"), str(tmp_path / "no-dir"), "", ""
    )
    assert bad["ready"] is False
    assert bad["errors"] == 3  # script + images + project folder
    levels = {row["label"]: row["level"] for row in bad["rows"]}
    assert levels["Script file"] == "error"
    assert levels["Images folder"] == "error"
    assert levels["Project folder"] == "error"
    assert "Fix 3 error(s)" in bad["summary_text"]


def test_pre_render_report_spec_format_list(tmp_path: Path) -> None:
    script = tmp_path / "notes.md"
    script.write_text("# markdown is not a supported script", encoding="utf-8")
    vm = _vm()
    report = vm.pre_render_report_model(
        str(script), str(tmp_path), str(tmp_path / "p"), ""
    )
    fmt = next(r for r in report["rows"] if r["label"] == "Script format")
    assert fmt["level"] == "error"
    assert "JSON" in fmt["value"]  # supported list shown verbatim
    assert report["ready"] is False


class _StubDriveModule:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def upload_file(self, path):
        self.calls.append(path)
        return dict(self.reply)


class _StubEngine:
    def __init__(self, module):
        self._module = module

    def module(self, name):
        return self._module if name == "drive_upload_engine" else None


def test_upload_render_to_drive_paths(tmp_path: Path) -> None:
    good = _StubDriveModule(
        {"success": True,
         "data": {"name": "v.mp4", "web_view_link": "https://x"}}
    )
    vm = _vm(engine=_StubEngine(good))
    ok, message = vm.upload_render_to_drive("/out/v.mp4")
    assert ok is True and "https://x" in message
    assert good.calls == ["/out/v.mp4"]
    skipped = _StubDriveModule(
        {"success": True, "data": {"skipped": "drive upload disabled"}}
    )
    ok, message = _vm(engine=_StubEngine(skipped)).upload_render_to_drive(
        "/out/v.mp4"
    )
    assert ok is False and "disabled" in message
    ok, message = _vm().upload_render_to_drive("/out/v.mp4")
    assert ok is False and "unavailable" in message
    crash = _StubDriveModule({"success": False, "error": "offline"})
    ok, message = _vm(engine=_StubEngine(crash)).upload_render_to_drive(
        "/out/v.mp4"
    )
    assert ok is False and "offline" in message
