"""Unit tests for core.core_engine.CoreEngine.

Registry-driven module loading (real importlib pass over all 20
registry modules), stage criticality (required aborts / optional
warns / disabled skips), the license gate, cancel + reentrancy guards,
and the full script->MP4 wiring with DI module fakes (RULE 1 seam:
modules never import each other; the orchestrator wires them).
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest  # noqa: F401

from core.core_engine import CoreEngine
from core.service_container import ServiceContainer


def _container(project_root: Path, tmp_path: Path) -> ServiceContainer:
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


def _wav(path: Path, seconds: float = 0.4, rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


class _FakeModule:
    """Minimal stand-in: canned responses + call recording."""

    def __init__(self, enabled: bool = True, **responses) -> None:
        self._enabled = enabled
        self.responses = responses
        self.calls: list = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def __getattr__(self, name):
        if name in ("_enabled", "responses", "calls", "enabled"):
            raise AttributeError(name)
        if name not in self.responses:
            raise AttributeError(name)

        def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            response = self.responses[name]
            if callable(response):
                return response(*args, **kwargs)
            return dict(response)

        return _call


def _ok(**data):
    payload = {"success": True, "data": data, "error": None, "warnings": []}
    return payload


def _fail(error: str):
    return {"success": False, "data": {}, "error": error, "warnings": []}


SCRIPT_DATA = {
    "project_settings": {"title": "Demo Documentary", "genre": "dark_history"},
    "voice_instructions": {"NARRATOR": "deep male"},
    "scenes": [
        {
            "scene_number": 1,
            "image": "hook.jpg",
            "transition_in": "crossfade",
            "transition_out": "crossfade",
            "animation": "ken_burns",
            "dialogue": [
                {"character": "NARRATOR", "emotion": "dramatic",
                 "text": "In the year 1347 darkness spread.", "pause_after": "short"}
            ],
        },
        {
            "scene_number": 2,
            "image": "map.jpg",
            "transition_in": "crossfade",
            "transition_out": "crossfade",
            "animation": "slow_zoom_in",
            "dialogue": [
                {"character": "NARRATOR", "emotion": "dramatic",
                 "text": "Nobody knew what was coming.", "pause_after": "short"}
            ],
        },
    ],
}


def _fake_registry(tmp_path: Path) -> dict:
    """DI module fakes for the full pipeline (no real engines)."""

    def _tts(text, profile, out_path, **kw):
        _wav(Path(str(out_path)), 0.4)
        return _ok(
            audio_path=str(out_path),
            duration=0.4,
            word_timestamps=[
                {"word": text.split()[0], "start": 0.0, "end": 0.2},
                {"word": text.split()[-1], "start": 0.2, "end": 0.35},
            ],
        )

    def _mix(project_id, out_path, settings=None):
        _wav(Path(str(out_path)), 1.2)
        return _ok(audio_path=str(out_path))

    def _srt(project_id, settings=None):
        srt = tmp_path / "out.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nDemo\n", encoding="utf-8")
        return _ok(srt_path=str(srt))

    def _burn(video, srt, style, output, **kw):
        Path(str(output)).write_bytes(b"FINAL-MP4")
        return _ok(output_path=str(output))

    def _render(scene, anim, grade, output, **kw):
        Path(str(output)).write_bytes(b"SEGMENT")
        return _ok(output_path=str(output))

    def _join(segments, timeline, output, **kw):
        Path(str(output)).write_bytes(b"JOINED-MP4")
        return _ok(output_path=str(output))

    def _timeline(project_id, narration_path=None, intro_config=None,
                  outro_config=None, save=True):
        return _ok(timeline={
            "total_duration": 0.8,
            "scenes": [
                {"scene_number": 1, "duration": 0.4},
                {"scene_number": 2, "duration": 0.4},
            ],
            "transitions": [],
        })

    return {
        "file_parser": _FakeModule(parse_script=_ok(**SCRIPT_DATA)),
        "tts_engine_manager": _FakeModule(generate_audio=_tts),
        "voice_profile_manager": _FakeModule(
            create_profiles_from_script=_ok(profiles_created=["NARRATOR"]),
            get_all_profiles=_ok(profiles=[]),
            load_profile=_ok(profile=None),
        ),
        "image_processor": _FakeModule(process_all_images=_ok(processed=2)),
        "audio_processor": _FakeModule(generate_final_mix=_mix),
        "subtitle_engine": _FakeModule(
            generate_srt_from_word_timestamps=_srt,
            burn_subtitles=_burn,
        ),
        "timeline_engine": _FakeModule(build_timeline=_timeline),
        "transition_engine": _FakeModule(),
        "animation_engine": _FakeModule(
            get_zoompan_filter=_ok(filter_string="zoompan=z='1'")
        ),
        "color_grade_engine": _FakeModule(
            build_grade_filter=_ok(filtergraph="eq=1")
        ),
        "sfx_engine": _FakeModule(
            load_sfx_library=_ok(present=1),
            auto_place_sfx=_ok(count=0),
            prepare_sfx_for_mixing=_ok(sfx_list=[], count=0),
        ),
        "intro_outro_engine": _FakeModule(
            generate_intro=_ok(skipped=True, kind="intro"),
            generate_outro=_ok(skipped=True, kind="outro"),
            get_intro_outro_settings=_ok(
                intro={"enabled": False, "duration": 5.0},
                outro={"enabled": False, "duration": 20.0},
            ),
        ),
        "export_engine": _FakeModule(
            render_scene_to_video=_render,
            join_segments_with_transitions=_join,
            verify_output=_ok(verified=True),
        ),
        "thumbnail_generator": _FakeModule(
            auto_generate_for_project=_ok(count=5, skipped=False)
        ),
        "drive_upload_engine": _FakeModule(
            upload_final_render=_ok(skipped="drive upload disabled (test)")
        ),
        "batch_engine": _FakeModule(),
        "quality_checker": _FakeModule(
            run_full_check=_ok(
                is_render_ready=True, passed=12, total_checks=12
            ),
            generate_report=lambda data: "OK",
        ),
        "channel_profile_manager": _FakeModule(
            apply_profile_to_project=_ok(updated_columns=["channel_profile_id"])
        ),
        "voice_store_manager": _FakeModule(),
        "keyword_analyzer": _FakeModule(analyze_all_scenes=_ok(analyzed=2)),
    }


def _engine(container: ServiceContainer, fakes: dict) -> CoreEngine:
    return CoreEngine(container, module_loader=fakes.get, auto_load=True)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------
@pytest.fixture
def container(project_root: Path, tmp_path: Path) -> ServiceContainer:
    return _container(project_root, tmp_path)


@pytest.fixture
def engine(container: ServiceContainer, tmp_path: Path) -> CoreEngine:
    engine = _engine(container, _fake_registry(tmp_path))
    return engine


def _run_script(engine: CoreEngine, tmp_path: Path, **kwargs) -> dict:
    kwargs.setdefault("enforce_license", False)
    return engine.run_script_pipeline(
        script_path=str(tmp_path / "script.txt"),
        project_folder=str(tmp_path / "project"),
        **kwargs,
    )


# ------------------------------------------------------------------
# Module loading
# ------------------------------------------------------------------
def test_loader_loads_all_registry_modules(container: ServiceContainer) -> None:
    engine = CoreEngine(container)  # real importlib loader
    status = engine.get_module_status()["data"]
    loaded = status["loaded_modules"]
    assert len(loaded) == 20  # every enabled registry entry instantiates
    for name in (
        "file_parser", "export_engine", "batch_engine", "core", "quality_checker",
    ):
        if name != "core":
            assert name in loaded
    report = status["report"]
    assert report["file_parser"]["priority"] == 1
    assert report["export_engine"]["required"] is True
    assert report["keyword_analyzer"]["required"] is False
    assert engine.module("file_parser") is not None
    assert engine.module("no_such_module") is None
    assert engine.is_optional_module() is False


def test_loader_records_broken_modules(
    container: ServiceContainer, tmp_path: Path
) -> None:
    fakes = _fake_registry(tmp_path)

    def _loader(name):
        if name == "sfx_engine":
            raise ImportError("broken sfx")
        return fakes.get(name)

    engine = CoreEngine(container, module_loader=_loader)
    report = engine.get_module_status()["data"]["report"]
    assert report["sfx_engine"]["loaded"] is False
    assert "broken sfx" in report["sfx_engine"]["error"]
    assert report["file_parser"]["loaded"] is True


def test_loader_skips_disabled_registry_entries(container: ServiceContainer) -> None:
    engine = CoreEngine(container, module_loader=lambda n: None, auto_load=False)
    engine._registry = lambda: [
        {"name": "disabled_one", "enabled": False, "required": False, "priority": 1},
        {"name": "enabled_one", "enabled": True, "required": False, "priority": 2},
    ]
    engine.load_modules()
    report = engine.get_module_status()["data"]["report"]
    assert report["disabled_one"]["loaded"] is False
    assert report["disabled_one"]["error"] == "disabled in registry"


# ------------------------------------------------------------------
# Happy path (script -> final mp4 wiring)
# ------------------------------------------------------------------
def test_full_script_pipeline_happy_path(engine: CoreEngine, tmp_path: Path) -> None:
    events = []
    for name in ("pipeline.started", "pipeline.stage_started",
                 "pipeline.stage_completed", "pipeline.completed"):
        engine.event_bus.subscribe(name, lambda d, n=name: events.append(n))
    result = _run_script(engine, tmp_path)
    assert result["success"] is True, result.get("error")
    data = result["data"]
    assert data["failed_stage"] is None
    assert data["output_file_path"] is not None
    assert Path(data["output_file_path"]).read_bytes() == b"FINAL-MP4"
    assert "Demo_Documentary" in data["output_file_path"]
    stage_names = [s["stage"] for s in data["stages"]]
    assert stage_names[0] == "license"
    assert stage_names[-1] == "drive_upload"
    stages_by_name = {s["stage"]: s["status"] for s in data["stages"]}
    assert stages_by_name["drive_upload"] == "skipped"  # self-skip (D.7)
    assert all(s["status"] in ("completed", "skipped", "warning")
               for s in data["stages"])

    pid = data["project_id"]
    project = engine.db.db.fetch_one(
        "SELECT * FROM projects WHERE id = ?", (pid,)
    )
    assert project["title"] == "Demo Documentary"
    assert project["status"] == "completed"
    assert int(project["render_count"]) == 1
    assert project["last_render_output_path"] == data["output_file_path"]
    scenes = engine.db.db.fetch_all(
        "SELECT * FROM scenes WHERE project_id = ? ORDER BY scene_number", (pid,)
    )
    assert len(scenes) == 2
    lines = engine.db.db.fetch_all(
        "SELECT * FROM dialogue_lines WHERE project_id = ?", (pid,)
    )
    assert len(lines) == 2
    assert all(int(l["audio_generated"]) == 1 for l in lines)
    assert all(l["audio_file_path"] for l in lines)
    assert events[0] == "pipeline.started"
    assert "pipeline.stage_started" in events
    assert events[-1] == "pipeline.completed"
    assert engine.get_state()["data"]["render_state"] == "COMPLETE"


def test_word_timestamps_are_narration_absolute(
    engine: CoreEngine, tmp_path: Path
) -> None:
    result = _run_script(engine, tmp_path)
    pid = result["data"]["project_id"]
    rows = engine.db.db.fetch_all(
        "SELECT start_time_ms, end_time_ms FROM word_timestamps"
        " WHERE project_id = ? ORDER BY start_time_ms",
        (pid,),
    )
    assert len(rows) == 4  # 2 words per line, 2 lines
    assert rows[0]["start_time_ms"] == 0
    assert rows[1]["end_time_ms"] == 350
    # line 2 offset = duration 0.4s + 0.25s narration pause
    assert rows[2]["start_time_ms"] == 650
    assert rows[3]["end_time_ms"] == 1000


def test_export_stage_joins_scene_segments_only(
    engine: CoreEngine, tmp_path: Path
) -> None:
    result = _run_script(engine, tmp_path)
    assert result["success"] is True
    export = engine.module("export_engine")
    join_calls = [c for c in export.calls if c[0] == "join_segments_with_transitions"]
    assert len(join_calls) == 1
    segments = join_calls[0][1][0]
    assert len(segments) == 2  # intro/outro were skipped=True in fakes
    render_calls = [c for c in export.calls if c[0] == "render_scene_to_video"]
    assert len(render_calls) == 2


def test_intro_outro_segments_are_joined_when_present(
    container: ServiceContainer, tmp_path: Path
) -> None:
    fakes = _fake_registry(tmp_path)

    def _segment(path):
        def _make(project_id, **kw):
            p = tmp_path / path
            p.write_bytes(b"IO")
            return _ok(segment_path=str(p), duration=5.0, skipped=False)
        return _make

    fakes["intro_outro_engine"] = _FakeModule(
        generate_intro=_segment("intro.mp4"),
        generate_outro=_segment("outro.mp4"),
        get_intro_outro_settings=_ok(
            intro={"enabled": True, "duration": 5.0},
            outro={"enabled": True, "duration": 20.0},
        ),
    )
    engine = _engine(container, fakes)
    result = _run_script(engine, tmp_path / "io_proj")
    (tmp_path / "io_proj").mkdir(exist_ok=True)
    result = engine.run_script_pipeline(
        script_path="s.txt",
        project_folder=str(tmp_path / "io_proj"),
        enforce_license=False,
    )
    assert result["success"] is True, result.get("error")
    export = engine.module("export_engine")
    join_calls = [c for c in export.calls if c[0] == "join_segments_with_transitions"]
    segments = join_calls[0][1][0]
    assert len(segments) == 4  # intro + 2 scenes + outro


# ------------------------------------------------------------------
# Stage criticality
# ------------------------------------------------------------------
def test_required_stage_failure_aborts(engine: CoreEngine, tmp_path: Path) -> None:
    export = engine.module("export_engine")
    export.responses["render_scene_to_video"] = _fail("encode exploded")
    failed = []
    engine.event_bus.subscribe("pipeline.failed", failed.append)
    result = _run_script(engine, tmp_path)
    assert result["success"] is False
    assert result["data"]["failed_stage"] == "export"
    assert "export" in result["error"]
    stage_names = [s["stage"] for s in result["data"]["stages"]]
    assert "thumbnails" not in stage_names  # abort stops the pipe
    assert failed and failed[0]["stage"] == "export"
    assert engine.get_state()["data"]["render_state"] == "FAILED"


def test_optional_stage_failure_warns_and_continues(
    engine: CoreEngine, tmp_path: Path
) -> None:
    engine.module("keyword_analyzer").responses["analyze_all_scenes"] = _fail(
        "keyword crash"
    )
    result = _run_script(engine, tmp_path)
    assert result["success"] is True
    keyword_stage = next(
        s for s in result["data"]["stages"] if s["stage"] == "keywords"
    )
    assert keyword_stage["status"] == "warning"
    assert any("keywords" in w for w in result["warnings"])


def test_missing_optional_module_skips(
    container: ServiceContainer, tmp_path: Path
) -> None:
    fakes = _fake_registry(tmp_path)
    del fakes["thumbnail_generator"]
    engine = _engine(container, fakes)
    result = _run_script(engine, tmp_path)
    assert result["success"] is True
    stage = next(
        s for s in result["data"]["stages"] if s["stage"] == "thumbnails"
    )
    assert stage["status"] == "skipped"


def test_disabled_optional_module_skips(
    engine: CoreEngine, tmp_path: Path
) -> None:
    engine.module("sfx_engine")._enabled = False
    result = _run_script(engine, tmp_path)
    assert result["success"] is True
    stage = next(s for s in result["data"]["stages"] if s["stage"] == "sfx")
    assert stage["status"] == "skipped"


def test_disabled_required_module_aborts(
    engine: CoreEngine, tmp_path: Path
) -> None:
    engine.module("export_engine")._enabled = False
    result = _run_script(engine, tmp_path)
    assert result["success"] is False
    assert result["data"]["failed_stage"] == "export"


def test_skip_stages_honored(engine: CoreEngine, tmp_path: Path) -> None:
    result = _run_script(
        engine, tmp_path, skip_stages=("sfx", "thumbnails")
    )
    assert result["success"] is True
    skipped = {
        s["stage"]
        for s in result["data"]["stages"]
        if s["status"] == "skipped" and s.get("reason") == "skip_stages"
    }
    assert skipped == {"sfx", "thumbnails"}
    thumbs = engine.module("thumbnail_generator")
    assert not [c for c in thumbs.calls if c[0] == "auto_generate_for_project"]


# ------------------------------------------------------------------
# Quality gate
# ------------------------------------------------------------------
def test_quality_gate_blocks_when_not_ready(
    engine: CoreEngine, tmp_path: Path
) -> None:
    engine.module("quality_checker").responses["run_full_check"] = _ok(
        is_render_ready=False, passed=9, total_checks=12
    )
    result = _run_script(engine, tmp_path, quality_gate=True)
    assert result["success"] is False
    assert result["data"]["failed_stage"] == "quality_gate"


def test_quality_is_advisory_by_default(engine: CoreEngine, tmp_path: Path) -> None:
    engine.module("quality_checker").responses["run_full_check"] = _ok(
        is_render_ready=False, passed=9, total_checks=12
    )
    result = _run_script(engine, tmp_path)  # quality_gate=False
    assert result["success"] is True
    assert any("NOT READY" in w for w in result["warnings"])


# ------------------------------------------------------------------
# License gate
# ------------------------------------------------------------------
class _LicenseStub:
    def __init__(self, status: str) -> None:
        self._status = status
        self.enabled = True

    def check_license(self):
        return {
            "success": True,
            "data": {"status": self._status, "days_remaining": 30},
            "error": None,
            "warnings": [],
        }


def test_license_expired_blocks_pipeline(
    engine: CoreEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        engine, "_license_instance", lambda ctx: _LicenseStub("expired")
    )
    result = engine.run_script_pipeline("s.txt", str(tmp_path / "p"))
    assert result["success"] is False
    assert result["data"]["failed_stage"] == "license"
    assert result["data"]["project_id"] is None  # abort before parse
    assert "expired" in result["error"]


def test_license_trial_allowed(engine: CoreEngine, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        engine, "_license_instance", lambda ctx: _LicenseStub("trial")
    )
    result = engine.run_script_pipeline("s.txt", str(tmp_path / "p"))
    assert result["success"] is True
    assert result["data"]["stages"][0]["status"] == "completed"


def test_license_enforcement_can_be_disabled(
    engine: CoreEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        engine, "_license_instance", lambda ctx: _LicenseStub("expired")
    )
    result = engine.run_script_pipeline(
        "s.txt", str(tmp_path / "p"), enforce_license=False
    )
    assert result["success"] is True
    assert result["data"]["stages"][0]["status"] == "skipped"


def test_license_module_unavailable_degrades_to_warning(
    engine: CoreEngine, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(engine, "_license_instance", lambda ctx: None)
    result = engine.run_script_pipeline("s.txt", str(tmp_path / "p"))
    assert result["success"] is True
    stage = result["data"]["stages"][0]
    assert stage["status"] == "warning"
    assert any("license" in w for w in result["warnings"])


# ------------------------------------------------------------------
# Cancel / reentrancy / project-only flow / batch
# ------------------------------------------------------------------
def test_cancel_during_tts_aborts_pipeline(
    container: ServiceContainer, tmp_path: Path
) -> None:
    fakes = _fake_registry(tmp_path)
    many_lines = dict(SCRIPT_DATA)
    many_lines["scenes"] = [dict(SCRIPT_DATA["scenes"][0])] * 3
    fakes["file_parser"] = _FakeModule(parse_script=_ok(**many_lines))
    engine = _engine(container, fakes)
    state = {"calls": 0}

    tts = engine.module("tts_engine_manager")
    original = tts.responses["generate_audio"]

    def _cancelling(*args):
        state["calls"] += 1
        if state["calls"] == 2:
            engine.cancel_pipeline()
        return original(*args)

    tts.responses["generate_audio"] = _cancelling
    result = _run_script(engine, tmp_path)
    assert result["success"] is False
    assert "cancelled" in result["error"]
    assert result["data"]["failed_stage"] == "tts"
    assert state["calls"] == 2  # third line never synthesized
    assert engine.get_state()["data"]["render_state"] == "CANCELLED"


def test_reentrancy_blocked(engine: CoreEngine, tmp_path: Path) -> None:
    engine._running = True
    result = engine.run_project_pipeline("any")
    assert result["success"] is False
    assert "already in progress" in result["error"]


def test_run_project_pipeline_missing_project(engine: CoreEngine) -> None:
    result = engine.run_project_pipeline("no-such-project")
    assert result["success"] is False
    assert "Project not found" in result["error"]


def test_run_project_pipeline_existing_project(
    engine: CoreEngine, tmp_path: Path
) -> None:
    first = _run_script(engine, tmp_path)
    pid = first["data"]["project_id"]
    again = engine.run_project_pipeline(pid, enforce_license=False)
    assert again["success"] is True
    assert again["data"]["project_id"] == pid
    stage = next(s for s in again["data"]["stages"] if s["stage"] == "parse")
    assert stage["status"] == "completed"  # reused existing project


def test_make_batch_processor(engine: CoreEngine, tmp_path: Path) -> None:
    processor = engine.make_batch_processor()
    bad = processor({"project_folder_path": str(tmp_path), "project_id": None})
    assert bad["success"] is False
    assert "project_id" in bad["error"]
    first = _run_script(engine, tmp_path)
    pid = first["data"]["project_id"]
    ok = processor(
        {"project_id": pid, "channel_profile_id": "profile_default"}
    )
    assert ok["success"] is True


def test_get_state_last_pipeline(engine: CoreEngine, tmp_path: Path) -> None:
    result = _run_script(engine, tmp_path)
    state = engine.get_state()["data"]
    assert state["pipeline_running"] is False
    last = state["last_pipeline"]
    assert last["project_id"] == result["data"]["project_id"]
    assert last["failed_stage"] is None


# ------------------------------------------------------------------
# D2a hotfix regressions (Windows D.3 smoke findings vs REAL ffmpeg)
# ------------------------------------------------------------------
def test_progress_forwarder_accepts_ffmpeg_three_arg_callback(
    engine: CoreEngine,
) -> None:
    """monitor_ffmpeg_progress calls callback(progress, fps, eta).

    The first REAL-ffmpeg run (Windows D.3) crashed the export stage
    because the forwarder accepted a single payload arg; POSIX fakes
    emit no frame= lines (unless FAKE_FRAMES=1) so tests never called
    it. The smoke test now sets FAKE_FRAMES=1 as the end-to-end net.
    """
    published: list = []
    engine.event_bus.subscribe(
        "pipeline.render_progress", lambda d: published.append(d)
    )
    forward = engine._progress_forwarder({"project_id": "p1"})
    forward(42.5, 59.94, 3.2)  # ffmpeg-style: three positional floats
    forward()  # bare call keeps working (all args defaulted)
    assert published[0] == {
        "project_id": "p1",
        "progress": 42.5,
        "fps": 59.94,
        "eta_seconds": 3.2,
    }
    assert published[1]["progress"] == 0.0


def test_image_assets_column_resolved_via_pragma_without_error_logs(
    engine: CoreEngine, caplog: "pytest.LogCaptureFixture"
) -> None:
    """The shipped schema has original_file_path (not file_path).

    The old probe-then-catch fallback made database_service emit a
    misleading ERROR per scene ("no such column: file_path") on every
    real render. PRAGMA resolution picks the true column silently and
    caches it (negative result included).
    """
    import logging

    assert engine._image_assets_column() == "original_file_path"
    engine._assets_col_cache = None  # force re-resolution below
    with caplog.at_level(logging.ERROR, logger="autopilot.database"):
        assert engine._processed_image_for("ghost.jpg") is None
        assert engine._processed_image_for("") is None
        assert engine._image_assets_column() == "original_file_path"
    assert "no such column" not in caplog.text
