"""Headless tests for the spec §8-§17 panel/dialog view-models.

Scene structure ops + undo/redo, storyboard/details/markers/wave
form models, grade + audio writes to REAL project columns, voice
store normalization (stub engine + DB fallback), batch queue DB
flow, import-ZIP, backup/autosave rotation, channel profile CRUD,
engine manager detection, first-run + key generator models, and
the notifications model. All without PyQt6, against the real schema.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from core.service_container import ServiceContainer
from core.time_helper import utc_now_str
from ui.viewmodel import UiViewModel, notification_model


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


def _seed_project(vm: UiViewModel, scenes: int = 3) -> str:
    db = vm.container.get("database").db
    now = utc_now_str()
    db.execute(
        "INSERT INTO projects (id, title, status, project_folder_path,"
        " narration_volume, music_volume, sfx_volume, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("p1", "Demo Doc", "completed", "/tmp/p1", 1.0, 0.4, 0.6,
         now, now),
    )
    for number in range(1, scenes + 1):
        db.execute(
            "INSERT INTO scenes (id, project_id, scene_number,"
            " scene_title, image_filename, image_file_path,"
            " image_matched, start_time, duration, chapter_title,"
            " animation_type, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"s{number}", "p1", number, f"Scene {number}",
                f"img{number}.jpg",
                "/tmp/img1.jpg" if number == 1 else None,
                1 if number == 1 else 0,
                (number - 1) * 10.0, 10.0,
                "The Fall" if number == 2 else "",
                "ken_burns", "completed", now, now,
            ),
        )
        db.execute(
            "INSERT INTO dialogue_lines (id, project_id, scene_id,"
            " line_number, character_name, emotion, text_content,"
            " status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"d{number}", "p1", f"s{number}", 1, "NARRATOR",
                "calm", f"line of scene {number}", "completed",
                now, now,
            ),
        )
    return "p1"


def _scene_titles(vm: UiViewModel) -> list:
    model = vm.timeline_model("p1")
    return [s["title"] for s in model["scenes"]]


# ------------------------------------------------------------------
# Scene ops + undo/redo (spec §5 Edit menu + §9 context menu)
# ------------------------------------------------------------------
def test_copy_paste_scene_inserts_copy(db_vm: UiViewModel) -> None:
    _seed_project(db_vm)
    ok, message = db_vm.copy_scene("p1", 2)
    assert ok and "Scene 2" in message
    ok, message = db_vm.paste_scene("p1", 2)
    assert ok and "#3" in message
    titles = _scene_titles(db_vm)
    assert titles == ["Scene 1", "Scene 2", "Scene 2", "Scene 3"]
    # copy carried its dialogue line
    pasted = db_vm.timeline_model("p1")["scenes"][2]
    assert pasted["lines"][0]["text"] == "line of scene 2"


def test_delete_scene_renumbers(db_vm: UiViewModel) -> None:
    _seed_project(db_vm)
    ok, message = db_vm.delete_scene("p1", 2)
    assert ok
    model = db_vm.timeline_model("p1")
    assert [s["number"] for s in model["scenes"]] == [1, 2]
    assert [s["title"] for s in model["scenes"]] == [
        "Scene 1", "Scene 3",
    ]


def test_reorder_scene_drag_drop_math(db_vm: UiViewModel) -> None:
    _seed_project(db_vm)
    ok, message = db_vm.reorder_scene("p1", 1, 2)
    assert ok and "position 3" in message
    assert _scene_titles(db_vm) == ["Scene 2", "Scene 3", "Scene 1"]
    assert [s["number"] for s in db_vm.timeline_model("p1")["scenes"]] \
        == [1, 2, 3]


def test_undo_redo_restores_structure(db_vm: UiViewModel) -> None:
    _seed_project(db_vm)
    db_vm.delete_scene("p1", 2)
    assert "delete scene" in db_vm.undo_label()
    ok, message = db_vm.undo()
    assert ok and "Scene 2" in _scene_titles(db_vm)
    model = db_vm.timeline_model("p1")
    assert [s["title"] for s in model["scenes"]] == [
        "Scene 1", "Scene 2", "Scene 3",
    ]
    ok, message = db_vm.redo()
    assert ok
    assert _scene_titles(db_vm) == ["Scene 1", "Scene 3"]
    ok, message = db_vm.undo()
    assert ok
    assert _scene_titles(db_vm) == [
        "Scene 1", "Scene 2", "Scene 3",
    ]
    fresh = UiViewModel({})
    assert fresh.undo()[0] is False
    assert fresh.redo()[1] == "Nothing to redo."


def test_scene_ops_guards(db_vm: UiViewModel) -> None:
    assert db_vm.copy_scene("p1", 1)[0] is False  # no project
    assert db_vm.paste_scene("p1")[0] is False  # empty clipboard
    assert UiViewModel({}).delete_scene("p1", 1)[0] is False


# ------------------------------------------------------------------
# Storyboard / details / markers / waveform (spec §8/§9)
# ------------------------------------------------------------------
def test_storyboard_and_scene_details(db_vm: UiViewModel) -> None:
    _seed_project(db_vm)
    board = db_vm.storyboard_model("p1")
    assert board["found"] is True and board["count"] == 3
    assert board["cards"][0]["thumb_path"] == "/tmp/img1.jpg"
    details = db_vm.scene_details_model("p1", 2)
    assert details["found"] is True
    rows = dict(details["rows"])
    assert rows["Animation"] == "ken_burns"
    assert "starts 0:10" in rows["Timing"]
    assert details["lines"][0]["text"] == "line of scene 2"
    missing = db_vm.scene_details_model("p1", 99)
    assert missing["found"] is False


def test_scene_at_position_and_markers(db_vm: UiViewModel) -> None:
    _seed_project(db_vm)
    scenes = db_vm.timeline_model("p1")["scenes"]
    assert db_vm.scene_at_position(scenes, 15)["number"] == 2
    assert db_vm.scene_at_position(scenes, 0)["number"] == 1
    assert db_vm.scene_at_position(scenes, 999)["number"] == 3
    markers = db_vm.chapter_markers("p1")["markers"]
    # one scene is chapter-flagged ("The Fall") -> markers show it,
    # positioned by its start time within the total duration
    assert [m["title"] for m in markers] == ["The Fall"]
    assert abs(markers[0]["percent"] - (10.0 / 30.0 * 100.0)) < 0.5


def test_chapter_markers_fallback_every_scene(
    db_vm: UiViewModel,
) -> None:
    db = db_vm.container.get("database").db
    now = utc_now_str()
    db.execute(
        "INSERT INTO projects (id, title, status, project_folder_path,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("p2", "No Chapters", "completed", "/tmp/p2", now, now),
    )
    for number in (1, 2):
        db.execute(
            "INSERT INTO scenes (id, project_id, scene_number,"
            " scene_title, start_time, duration, status, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"x{number}", "p2", number, f"Scene {number}",
             (number - 1) * 10.0, 10.0, "completed", now, now),
        )
    markers = db_vm.chapter_markers("p2")["markers"]
    assert len(markers) == 2
    assert markers[0]["percent"] == 0.0  # strip starts at 0
    assert abs(markers[1]["percent"] - 50.0) < 0.5


def test_waveform_model_honest_states(
    db_vm: UiViewModel, tmp_path: Path
) -> None:
    _seed_project(db_vm)
    empty = db_vm.waveform_model("p1")
    assert empty["found"] is False and "after render" in empty["note"]
    # a real PCM .wav track -> live peaks, decoded with stdlib wave
    import wave
    import struct

    wav_path = tmp_path / "narr.wav"
    with wave.open(str(wav_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        frames = b"".join(
            struct.pack("<h", 8000 if i % 40 < 20 else -8000)
            for i in range(3200)
        )
        handle.writeframes(frames)
    db_vm.container.get("database").db.execute(
        "INSERT INTO audio_tracks (id, project_id, track_type,"
        " file_path, file_name, duration_seconds, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("a1", "p1", "narration", str(wav_path), "narr.wav", 0.4,
         utc_now_str()),
    )
    model = db_vm.waveform_model("p1")
    assert model["found"] is True
    assert len(model["peaks"]) >= 8
    assert max(model["peaks"]) > 0.2


# ------------------------------------------------------------------
# Grade / animation / transition / export (spec §10)
# ------------------------------------------------------------------
def test_grade_options_and_apply(db_vm: UiViewModel) -> None:
    project_id = _seed_project(db_vm)
    presets = db_vm.color_presets()
    assert any(p["id"] == "dark_moody" for p in presets)  # real config
    sliders = db_vm.grade_sliders()
    assert [s["key"] for s in sliders] == [
        "brightness", "contrast", "saturation", "vignette", "film_grain",
    ]
    override = db_vm.grade_override(
        {"brightness": 999, "contrast": 120, "saturation": 80,
         "vignette": 40, "film_grain": 10},
        lut="dark_moody.cube", lut_opacity=60,
    )
    assert override["brightness"] == 0.5  # clamped, not trusted
    assert override["lut_opacity"] == 0.6
    assert override["vignette_enabled"] is True
    ok, message = db_vm.apply_grade_to_all(project_id, override)
    assert ok and "3 scene(s)" in message
    row = db_vm.container.get("database").db.fetch_one(
        "SELECT color_grade_override FROM scenes WHERE id = 's1'"
    )
    payload = json.loads(row["color_grade_override"])
    assert payload["lut_file"] == "dark_moody.cube"
    ok, message = db_vm.apply_grade_preset(project_id, "dark_moody")
    assert ok
    row = db_vm.container.get("database").db.fetch_one(
        "SELECT color_grade_preset FROM projects WHERE id = 'p1'"
    )
    assert row["color_grade_preset"] == "dark_moody"
    ok, message = db_vm.set_export_preset(project_id, "youtube_4k")
    assert ok
    assert UiViewModel({}).apply_grade_to_all("p1", {})[0] is False


def test_animation_transition_apply(db_vm: UiViewModel) -> None:
    _seed_project(db_vm)
    options = db_vm.animation_options()
    assert "ken_burns" in options["animations"]
    assert "medium" in options["intensities"]
    ok, message = db_vm.apply_scene_animation(
        "p1", 2, "slow_zoom_in", "dramatic"
    )
    assert ok and "Scene 2" in message
    row = db_vm.container.get("database").db.fetch_one(
        "SELECT animation_type, animation_intensity FROM scenes"
        " WHERE id = 's2'"
    )
    assert row["animation_type"] == "slow_zoom_in"
    assert row["animation_intensity"] == "dramatic"
    transitions = db_vm.transition_options()
    assert any(t["id"] == "fade" for t in transitions)  # real config
    ok, message = db_vm.apply_scene_transition(
        "p1", 0, "fade_black", "fade", 1.2
    )
    assert ok and "Project default" in message
    row = db_vm.container.get("database").db.fetch_one(
        "SELECT default_transition FROM projects WHERE id = 'p1'"
    )
    assert row["default_transition"] == "fade_black"


# ------------------------------------------------------------------
# Audio settings round-trip (spec §11)
# ------------------------------------------------------------------
def _record_config_set(db_vm: UiViewModel, monkeypatch) -> list:
    """Instance-level recorder: real config FILE stays untouched."""
    calls = []
    config = db_vm.container.get("config")
    monkeypatch.setattr(
        config, "set", lambda key, value: calls.append((key, value))
    )
    return calls


def test_audio_settings_write_real_columns(
    db_vm: UiViewModel, monkeypatch
) -> None:
    _seed_project(db_vm)
    model = db_vm.audio_settings("p1")
    assert model["found"] is True
    assert model["narration_volume"] == 100
    calls = _record_config_set(db_vm, monkeypatch)
    ok, message = db_vm.save_audio_settings(
        "p1",
        {
            "narration_volume": 150, "music_volume": 25,
            "sfx_volume": 80, "music_file_path": "/music/dark.mp3",
            "ducking_enabled": False, "ducking_depth": 70,
        },
    )
    assert ok
    row = db_vm.container.get("database").db.fetch_one(
        "SELECT narration_volume, music_volume, sfx_volume,"
        " music_file_path FROM projects WHERE id = 'p1'"
    )
    assert abs(row["narration_volume"] - 1.5) < 0.001
    assert abs(row["music_volume"] - 0.25) < 0.001
    assert row["music_file_path"] == "/music/dark.mp3"
    assert ("ducking_enabled", False) in calls
    assert ("ducking_depth", 70) in calls
    assert UiViewModel({}).audio_settings("p1")["found"] is False


# ------------------------------------------------------------------
# Voice store (spec §13)
# ------------------------------------------------------------------
class _StubVoiceModule:
    def __init__(self) -> None:
        self.installed = []

    def list_voices(self):
        return {
            "success": True,
            "data": {
                "voices": [
                    {"id": "v1", "display_name": "Deep Male EN",
                     "engine": "piper", "language": "en",
                     "gender": "male", "quality_rating": 5,
                     "file_size_mb": 62.0, "is_installed": 1,
                     "description": "narration"},
                    {"id": "v2", "display_name": "Calm Female HI",
                     "engine": "piper", "language": "hi",
                     "gender": "female", "quality_rating": 4,
                     "file_size_mb": 58.0, "is_installed": 0,
                     "description": "hindi narration"},
                ]
            },
        }

    def install_voice(self, voice_id):
        self.installed.append(voice_id)
        return {"success": True, "data": {"voice_id": voice_id}}

    def uninstall_voice(self, voice_id):
        return {"success": True, "data": {"voice_id": voice_id}}


class _StubEngine:
    def __init__(self, module):
        self._module = module

    def module(self, name):
        return self._module


def test_voice_store_model_filters_and_actions() -> None:
    module = _StubVoiceModule()
    vm = UiViewModel({"engine": _StubEngine(module)})
    model = vm.voice_store_model()
    assert model["count"] == 2 and model["installed_count"] == 1
    assert model["voices"][0]["name"] == "Deep Male EN"
    assert model["voices"][0]["quality"] == 5
    female = vm.voice_store_model(gender="female")
    assert female["count"] == 1
    hindi = vm.voice_store_model(query="hindi")
    assert hindi["voices"][0]["id"] == "v2"
    ok, message = vm.voice_install("v2")
    assert ok and module.installed == ["v2"]
    ok, message = vm.voice_remove("v1")
    assert ok
    bare = UiViewModel({})
    assert bare.voice_install("v1")[0] is False  # honest unavailable


# ------------------------------------------------------------------
# Batch queue (spec §14) — real batch_queue table flow
# ------------------------------------------------------------------
def test_batch_queue_flow(db_vm: UiViewModel, tmp_path: Path) -> None:
    script = tmp_path / "story.txt"
    script.write_text("SCENE 1", encoding="utf-8")
    assert db_vm.batch_model()["count"] == 0
    ok, message = db_vm.batch_add(
        str(script), str(tmp_path), str(tmp_path / "proj"), "Doc A", 3
    )
    assert ok
    ok, message = db_vm.batch_add(
        str(script), str(tmp_path), str(tmp_path / "proj2"), "Doc B", 1
    )
    assert ok
    model = db_vm.batch_model()
    assert model["count"] == 2 and model["queued"] == 2
    assert model["rows"][0]["title"] == "Doc B"  # P1 runs first
    first_id = model["rows"][0]["id"]
    ok, message = db_vm.batch_move(first_id, 2)  # P1 -> P3
    assert ok and "Priority set to 3" in message
    assert db_vm.batch_model()["rows"][0]["title"] == "Doc A"
    db_vm.batch_set_status(first_id, "failed", error="boom")
    row = db_vm.container.get("database").db.fetch_one(
        "SELECT status, error_message FROM batch_queue WHERE id = ?",
        (first_id,),
    )
    assert row["status"] == "failed" and row["error_message"] == "boom"
    ok, message = db_vm.batch_remove(first_id)
    assert ok  # queued-only guard: failed stays? queued item was failed
    model = db_vm.batch_model()
    assert any(r["status"] == "failed" for r in model["rows"])
    ok, message = db_vm.batch_add(
        str(tmp_path / "gone.txt"), "", "", "Nope", 5
    )
    assert ok is False and "script" in message


# ------------------------------------------------------------------
# Import ZIP / backup / autosave / profiles / engines / first-run
# ------------------------------------------------------------------
def test_import_zip_stages_files(
    db_vm: UiViewModel, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    bundle = tmp_path / "doc_bundle.zip"
    with zipfile.ZipFile(str(bundle), "w") as archive:
        archive.writestr("script.txt", "A dark script body")
        archive.writestr("pics/one.jpg", b"\xff\xd8one")
        archive.writestr("pics/two.png", b"\x89PNGtwo")
    result = db_vm.import_zip(str(bundle))
    assert result["success"] is True
    assert result["copied"] == 3
    assert result["script_path"].endswith("script.txt")
    assert Path(result["images_folder"]).is_dir()
    assert (Path(result["project_folder"]) / "imports").is_dir()
    bad = db_vm.import_zip(str(tmp_path / "missing.zip"))
    assert bad["success"] is False


def test_backup_and_autosave_rotation(
    db_vm: UiViewModel, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)  # backups/ lands in tmp, not the repo
    ok, message = db_vm.backup_now()
    assert ok and "autopilot_backup_" in message
    for _round in range(4):
        ok, message = db_vm.autosave_tick()
    assert ok
    autosaves = sorted(Path("backups").glob("autosave_*.zip"))
    assert [p.name for p in autosaves] == [
        "autosave_1.zip", "autosave_2.zip", "autosave_3.zip",
    ]
    with zipfile.ZipFile(str(autosaves[0])) as archive:
        assert "database/autopilot.db" in archive.namelist()
    assert UiViewModel({}).backup_now()[0] is False


def test_channel_profile_crud(db_vm: UiViewModel, monkeypatch) -> None:
    rows = db_vm.channel_profile_rows()
    if not rows:
        pytest.skip("schema ships no seed channel profiles here")
    first = rows[0]
    ok, message = db_vm.channel_profile_duplicate(first["id"])
    assert ok and "copy" in message
    names = [r["name"] for r in db_vm.channel_profile_rows()]
    assert any("copy" in name for name in names)
    calls = _record_config_set(db_vm, monkeypatch)
    ok, message = db_vm.channel_profile_set_default(first["id"])
    assert ok
    assert ("default_channel_profile", first["id"]) in calls
    dup = next(
        r for r in db_vm.channel_profile_rows() if "copy" in r["name"]
    )
    ok, message = db_vm.channel_profile_delete(dup["id"])
    assert ok
    assert UiViewModel({}).channel_profile_rows() == []


def test_engine_manager_first_run_keygen(
    db_vm: UiViewModel, monkeypatch
) -> None:
    engines = db_vm.engine_install_model()
    assert {r["name"] for r in engines["rows"]} == {
        "FFmpeg", "FFprobe", "Piper TTS",
    }
    assert engines["missing"]  # bare test container: nothing found
    first = db_vm.first_run_model()
    assert first["needs_wizard"] is True
    calls = _record_config_set(db_vm, monkeypatch)
    db_vm.mark_first_run_done()
    assert ("first_run_wizard_done", True) in calls
    keys = db_vm.key_generator_model()
    assert keys["available"] is False  # honest: admin tool boundary
    assert "machine ID" in keys["message"] or "admin" in keys["message"]
    assert keys["license_status"]


# ------------------------------------------------------------------
# Notifications (spec §17)
# ------------------------------------------------------------------
def test_notification_model_types() -> None:
    info = notification_model("info", "hello")
    assert info["timeout_ms"] == 4000  # spec: auto-dismiss 4s
    assert info["icon"] and info["level"] == "info"
    assert notification_model("success", "x")["icon"] == "✓"
    assert notification_model("warning", "x")["icon"] == "⚠"
    assert notification_model("error", "x")["icon"] == "✗"
    weird = notification_model("loud", "x")
    assert weird["level"] == "info"  # unknown -> info, never crash
