"""D.1 sandbox smoke test: real modules, real wiring, fake FFmpeg.

Runs the full core_engine script pipeline against the repo's
sample_project fixture with EVERY real module (no DI fakes) and the
cross-platform fake ffmpeg/ffprobe doubles. This proves orchestration:
parse -> profiles -> keywords -> quality -> images -> TTS (synthetic
fallback) -> SFX -> mix -> SRT -> intro/outro -> timeline -> per-scene
render -> join + audio mux -> subtitle burn -> thumbnails -> complete.

Two deliberate limitations of the sandbox smoke (honest callouts):
* The "verify" stage is skipped: the fake ffprobe reports a constant
  duration, which can never match the real timeline total within the
  +-1s tolerance. Real ffprobe runs it on Windows (D.3 milestone).
* Output bytes are the fake double's FAKEMP4DATA, not a real H.264
  stream. Pixel/bitstream proof = the Windows smoke with real FFmpeg.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.core_engine import CoreEngine
from core.service_container import ServiceContainer


@pytest.fixture
def container(
    project_root: Path,
    tmp_path: Path,
    fake_ffmpeg_factory,
    fake_ffprobe_factory,
    monkeypatch,
) -> ServiceContainer:
    fake = fake_ffmpeg_factory(tmp_path, tmp_path / "ffmpeg.log")
    fake_ffprobe_factory(tmp_path)
    monkeypatch.setenv("FAKE_PROBE_DURATION", "8.0")
    # D2a net: emit fake ffmpeg frame=/fps= progress lines so the
    # export stage's monitor -> CoreEngine._progress_forwarder path is
    # exercised (the Windows D.3 smoke crashed it with REAL ffmpeg).
    monkeypatch.setenv("FAKE_FRAMES", "1")
    return ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp_path / "autopilot.db"),
            "schema_path": str(project_root / "database" / "schema.sql"),
            "config_folder": str(project_root / "config"),
            "cache_folder": str(tmp_path / "cache"),
            "log_folder": str(tmp_path / "logs"),
            "ffmpeg_path": str(fake),
        },
        project_root=project_root,
    )


@pytest.mark.slow
def test_smoke_real_pipeline_renders_final_file(
    container: ServiceContainer, project_root: Path, tmp_path: Path
) -> None:
    fixture = project_root / "tests" / "fixtures" / "sample_project"
    engine = CoreEngine(container)
    status = engine.get_module_status()["data"]
    assert len(status["loaded_modules"]) == 20

    events = []
    for name in (
        "pipeline.started", "pipeline.stage_completed", "pipeline.completed",
    ):
        engine.event_bus.subscribe(name, lambda d, n=name: events.append(n))
    render_progress: list = []
    engine.event_bus.subscribe(
        "pipeline.render_progress", lambda d: render_progress.append(d)
    )

    result = engine.run_script_pipeline(
        script_path=str(fixture / "script" / "sample_script.txt"),
        project_folder=str(tmp_path / "smoke_project"),
        images_folder=str(fixture / "images"),
        enforce_license=False,
        skip_stages=("verify",),  # fake ffprobe duration is a constant
    )
    assert result["success"] is True, f"{result.get('error')} | {result['warnings']}"

    data = result["data"]
    stages = {s["stage"]: s["status"] for s in data["stages"]}
    assert stages["parse"] == "completed"
    assert stages["voice_profiles"] == "completed"
    assert stages["images"] == "completed"
    assert stages["tts"] == "completed"
    assert stages["audio_mix"] == "completed"
    assert stages["timeline"] == "completed"
    assert stages["export"] == "completed"
    assert stages["burn_subtitles"] == "completed"
    assert stages["verify"] == "skipped"
    assert stages["thumbnails"] == "completed"

    output = Path(data["output_file_path"])
    assert output.exists() and output.stat().st_size > 0
    assert output.suffix == ".mp4"
    assert "The_Dark_History_of_the_Black_Death" in output.name

    project_id = data["project_id"]
    db = container.get("database")
    project = db.db.fetch_one(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    )
    assert project["status"] == "completed"
    assert project["last_render_output_path"] == str(output)
    assert int(project["render_count"]) == 1
    assert db.db.get_table_row_count("scenes") >= 2
    assert db.db.get_table_row_count("dialogue_lines") >= 2
    assert db.db.get_table_row_count("voice_profiles") >= 2  # NARRATOR+HISTORIAN
    assert db.db.get_table_row_count("word_timestamps") >= 4
    assert db.db.get_table_row_count("thumbnails") >= 1
    timeline = db.db.fetch_one(
        "SELECT timeline_json FROM timeline_data WHERE project_id = ?",
        (project_id,),
    )
    assert timeline is not None and timeline["timeline_json"]

    assert events[0] == "pipeline.started"
    assert events[-1] == "pipeline.completed"
    assert engine.get_state()["data"]["render_state"] == "COMPLETE"

    # D2a: fake FAKE_FRAMES lines must actually drive the forwarder
    # (both fake variants print 5 frame=/fps=60.0 lines per encode)
    assert render_progress, "no render progress events reached the bus"
    probe = render_progress[0]
    assert "progress" in probe and "fps" in probe and "eta_seconds" in probe

    # the fake ffmpeg was actually exercised by multiple stages
    log = (tmp_path / "ffmpeg.log").read_text(encoding="utf-8")
    assert log.count("CMD ") >= 5  # intro, scenes, join, mux, burn...
