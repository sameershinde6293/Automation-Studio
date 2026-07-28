"""Unit tests for ui.viewmodel.UiViewModel (Qt-free by design).

Everything here runs headless — no PyQt6 import anywhere. The shell
in ui/app.py merely paints what these tests pin down.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.core_engine import CoreEngine
from core.event_bus import EventBus
from core.service_container import ServiceContainer
from ui.viewmodel import PIPELINE_EVENTS, UiViewModel


class _StubEngine:
    """Duck-typed CoreEngine stand-in (the viewmodel's contract)."""

    def __init__(self) -> None:
        self.event_bus = EventBus()
        self.runs = []
        self.cancelled = 0

    def stage_names(self):
        return CoreEngine.stage_names()

    def get_module_status(self):
        return {"success": True, "data": {"loaded_modules": ["m1", "m2"]}}

    def cancel_pipeline(self):
        self.cancelled += 1
        return {"success": True}

    def run_script_pipeline(self, **kwargs):
        self.runs.append(kwargs)
        return {"success": True, "data": {"output_file_path": "/tmp/o.mp4"}}


def _vm(**ctx) -> UiViewModel:
    return UiViewModel(ctx)


# ------------------------------------------------------------------
# Identity / status
# ------------------------------------------------------------------
def test_window_title_contains_license_state() -> None:
    vm = _vm(license_data={"status": {"status": "trial", "days_remaining": 4}})
    assert "Autopilot" in vm.window_title()
    assert "trial" in vm.window_title()


def test_engine_ready_and_module_count() -> None:
    assert _vm().engine_ready() is False
    assert _vm().module_count() == 0
    vm = _vm(engine=_StubEngine())
    assert vm.engine_ready() is True
    assert vm.module_count() == 2


def test_stage_names_mirror_core_stage_plan() -> None:
    names = _vm(engine=_StubEngine()).stage_names()
    assert names == CoreEngine.stage_names()
    assert names[0] == "license"
    assert names[-1] == "drive_upload"
    assert len(names) == 18
    assert _vm().stage_names() == []


# ------------------------------------------------------------------
# License summary
# ------------------------------------------------------------------
def test_license_summary_trial_from_ctx() -> None:
    summary = _vm(
        license_data={"status": {"status": "trial", "days_remaining": 11}}
    ).license_summary()
    assert summary["status"] == "trial"
    assert summary["days_remaining"] == 11
    assert "11" in summary["message"]


def test_license_summary_falls_back_to_manager() -> None:
    manager = SimpleNamespace(
        check_license=lambda: {"success": True, "data": {"status": "active"}},
        get_days_remaining=lambda: 0,
    )
    summary = _vm(license=manager).license_summary()
    assert summary["status"] == "active"
    assert "Licensed" in summary["message"]


def test_license_summary_never_crashes() -> None:
    summary = _vm(license_data={"status": "bogus"}).license_summary()
    assert summary["status"] in ("bogus", "unknown")
    assert summary["message"]


# ------------------------------------------------------------------
# Render form
# ------------------------------------------------------------------
def test_validate_render_inputs(tmp_path: Path) -> None:
    vm = _vm()
    ok, message = vm.validate_render_inputs("", str(tmp_path), str(tmp_path))
    assert ok is False and "script" in message.lower()
    script = tmp_path / "s.txt"
    script.write_text("//TITLE: x", encoding="utf-8")
    ok, message = vm.validate_render_inputs(str(script), "", str(tmp_path))
    assert ok is False and "images" in message.lower()
    ok, message = vm.validate_render_inputs(
        str(script), str(tmp_path), str(tmp_path / "proj")
    )
    assert ok is True and message == ""
    assert (tmp_path / "proj").is_dir()  # creates the folder


def test_build_render_request_maps_exact_engine_contract(
    tmp_path: Path,
) -> None:
    script = tmp_path / "dark_history.txt"
    script.write_text("x", encoding="utf-8")
    request = _vm().build_render_request(
        str(script), str(tmp_path), str(tmp_path / "p"),
        title="", export_preset="", quality_gate=True,
    )
    assert request["script_path"] == str(script)
    assert request["title"] == "dark history"  # falls back to stem
    assert request["export_preset"] is None  # '' normalized away
    assert request["quality_gate"] is True
    assert request["enforce_license"] is True


# ------------------------------------------------------------------
# Event normalization + subscription
# ------------------------------------------------------------------
def test_normalize_event_progress_rounds_and_formats() -> None:
    record = UiViewModel.normalize_event(
        "pipeline.render_progress",
        {"progress": 12.3456, "fps": 59.4, "eta_seconds": 7.8},
    )
    assert record["percent"] == 12.3
    assert "12.3%" in record["text"] and "ETA 8s" in record["text"]


def test_normalize_event_lifecycle() -> None:
    started = UiViewModel.normalize_event("pipeline.stage_started",
                                          {"stage": "tts"})
    assert "Synthesising narration" in started["text"]
    failed = UiViewModel.normalize_event("pipeline.failed",
                                         {"stage": "export"})
    assert failed["level"] == "error" and "export" in failed["text"]
    done = UiViewModel.normalize_event(
        "pipeline.completed", {"output_file_path": "/out/v.mp4"}
    )
    assert done["percent"] == 100.0 and "/out/v.mp4" in done["text"]
    weird = UiViewModel.normalize_event("pipeline.other", None)
    assert weird["event"] == "pipeline.other" and weird["percent"] is None


def test_subscribe_pipeline_forwards_normalized_and_unsubscribes() -> None:
    engine = _StubEngine()
    records = []
    unsubscribe = _vm(engine=engine).subscribe_pipeline(records.append)
    engine.event_bus.publish(
        "pipeline.render_progress", {"progress": 1.0, "fps": 1.0}
    )
    engine.event_bus.publish("pipeline.stage_completed", {"stage": "images"})
    assert [r["event"] for r in records] == [
        "pipeline.render_progress", "pipeline.stage_completed",
    ]
    unsubscribe()
    engine.event_bus.publish("pipeline.stage_completed", {"stage": "sfx"})
    assert len(records) == 2  # silence after unsubscribe


def test_subscribe_pipeline_without_engine_is_noop() -> None:
    unsubscribe = _vm().subscribe_pipeline(lambda record: None)
    unsubscribe()  # must not raise
    assert "pipeline.started" in PIPELINE_EVENTS


# ------------------------------------------------------------------
# Projects list against the real schema
# ------------------------------------------------------------------
def test_refresh_projects_reads_real_db(
    project_root: Path, tmp_path: Path
) -> None:
    container = ServiceContainer.create_production_container(
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
    db = container.get("database").db
    for idx, stamp in enumerate(("2026-07-15", "2026-07-17", "2026-07-16")):
        db.execute(
            "INSERT INTO projects (id, title, status, created_at,"
            " updated_at, project_folder_path)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (f"p{idx}", f"Doc {idx}", "completed", stamp, stamp, "/tmp/p"),
        )
    rows = _vm(container=container).refresh_projects(limit=2)
    assert len(rows) == 2  # limit honoured
    assert rows[0]["title"] == "Doc 1"  # newest updated_at first
    assert rows[1]["updated_at"] == "2026-07-16"
    assert _vm().refresh_projects() == []  # no container -> graceful


@pytest.mark.parametrize("missing", [None])
def test_refresh_projects_missing_db_returns_empty(missing) -> None:
    assert _vm(container=missing).refresh_projects() == []


# ------------------------------------------------------------------
# D.6 data providers
# ------------------------------------------------------------------
def test_export_presets_from_real_config(project_root: Path, tmp_path: Path) -> None:
    container = ServiceContainer.create_production_container(
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
    presets = _vm(container=container).export_presets()
    ids = [p["id"] for p in presets]
    assert "youtube_1080p" in ids
    default_rows = [p for p in presets if "(default)" in p["label"]]
    assert len(default_rows) == 1  # exactly one flagged default
    assert _vm().export_presets() == []  # no container -> graceful


def test_channel_profiles_and_request_passthrough(
    project_root: Path, tmp_path: Path
) -> None:
    container = ServiceContainer.create_production_container(
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
    db = container.get("database").db
    db.execute(
        "INSERT INTO channel_profiles (id, profile_name, created_at,"
        " updated_at) VALUES (?, ?, ?, ?)",
        ("cp1", "Midnight Docs", "2026-07-17", "2026-07-17"),
    )
    profiles = _vm(container=container).channel_profiles()
    # seeded 'default' profile + our row; every entry is id+label
    assert {"id": "cp1", "label": "Midnight Docs"} in profiles
    assert all(set(p) == {"id", "label"} and p["id"] for p in profiles)
    request = _vm(container=container).build_render_request(
        "s.txt", str(tmp_path), str(tmp_path / "p"), channel_profile_id="cp1"
    )
    assert request["channel_profile_id"] == "cp1"
    assert _vm().build_render_request(
        "s.txt", "i", "p"
    )["channel_profile_id"] is None  # default None passthrough


def test_engines_status_and_activate_license() -> None:
    manager = SimpleNamespace(
        activate_license=lambda key: (
            {"success": False, "error": "invalid key"}
            if key == "BAD"
            else {"success": True, "data": {"status": {"status": "active"}}}
        )
    )
    vm = _vm(license=manager, engine=_StubEngine())
    status = vm.engines_status()
    assert set(status) >= {"ffmpeg", "ffprobe", "piper", "modules_loaded"}
    assert status["modules_loaded"] == 2
    ok, message = vm.activate_license("BAD")
    assert ok is False and "invalid key" in message
    ok, message = vm.activate_license("GOOD-1")
    assert ok is True and "activated" in message
    assert vm.activate_license("")[0] is False


def test_engines_status_counts_plugins() -> None:
    class _WithPlugins(_StubEngine):
        def plugin_names(self):
            return ["a", "b"]

    status = _vm(engine=_WithPlugins()).engines_status()
    assert status["plugins_loaded"] == 2
    assert status["modules_loaded"] == 2
    bare = _vm(engine=_StubEngine()).engines_status()
    assert bare["plugins_loaded"] == 0  # engine without plugin seam


def test_theme_palette_is_qt_free_and_branded() -> None:
    from ui import theme

    assert "#" in theme.ACCENT and "#" in theme.WINDOW_BG
    assert "QProgressBar::chunk" in theme.DARK_QSS
    assert "QListWidget#navList" in theme.DARK_QSS
    assert theme._NAV_WIDTH >= 100
