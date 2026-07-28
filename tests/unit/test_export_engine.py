"""Unit tests for modules.export_engine.ExportEngine.

Execution covered with fake ffmpeg/ffprobe executables (argv logging,
frame= progress lines, encoder probes, tiny-encode simulation). The
fakes are cross-platform test doubles provided by tests/conftest.py
(bash on POSIX, Python + subprocess shim on Windows). Crash recovery
tested against the real render_progress table.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest
from PIL import Image

from core.service_container import ServiceContainer
from modules.export_engine import EXPORT_PRESETS, ExportEngine

PROJECT = "proj-export-1"
PRESET_COUNT = 4


@pytest.fixture()
def fake_bins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_ffmpeg_factory: Callable[..., Path],
    fake_ffprobe_factory: Callable[..., Path],
) -> Path:
    """Fake ffmpeg + ffprobe in tmp_path; returns ffmpeg path."""
    log = tmp_path / "ffmpeg.log"
    monkeypatch.setenv("FAKE_FFMPEG_LOG", str(log))
    ffmpeg = fake_ffmpeg_factory(tmp_path, log)
    fake_ffprobe_factory(tmp_path)
    return ffmpeg


def _container(
    project_root: Path, tmp_path: Path, ffmpeg_path: str = "ffmpeg"
) -> ServiceContainer:
    return ServiceContainer.create_production_container(
        app_config={
            "database_path": str(tmp_path / "autopilot.db"),
            "schema_path": str(project_root / "database" / "schema.sql"),
            "config_folder": str(project_root / "config"),
            "cache_folder": str(tmp_path / "cache"),
            "log_folder": str(tmp_path / "logs"),
            "ffmpeg_path": ffmpeg_path,
        },
        project_root=project_root,
    )


@pytest.fixture()
def engine(project_root: Path, tmp_path: Path) -> ExportEngine:
    """Export engine WITHOUT a resolvable ffmpeg (software-free environment)."""
    return ExportEngine(_container(project_root, tmp_path))


@pytest.fixture()
def engine_ff(project_root: Path, tmp_path: Path, fake_bins: Path) -> ExportEngine:
    """Export engine wired to the fake ffmpeg/ffprobe pair."""
    return ExportEngine(
        _container(project_root, tmp_path, str(fake_bins.parent / "ffmpeg"))
    )


def _scene_image(tmp_path: Path, name: str = "img.png") -> Path:
    path = tmp_path / name
    Image.new("RGB", (64, 64), (30, 30, 60)).save(path)
    return path


def _segments(
    tmp_path: Path, durations: list[float], make_files: bool = True
) -> list[dict]:
    segs = []
    for i, dur in enumerate(durations):
        p = tmp_path / f"seg_{i:03d}.mp4"
        if make_files:
            p.write_bytes(b"FAKESEG" * 100)
        segs.append({"path": p, "duration": dur})
    return segs


def _argv(tmp_path: Path) -> str:
    return (tmp_path / "ffmpeg.log").read_text(encoding="utf-8")


def _seed_project(engine: ExportEngine, project_id: str) -> None:
    now = "2026-07-16 00:00:00"
    engine.db.db.execute(
        "INSERT INTO projects (id, title, created_at, updated_at, project_folder_path)"
        " VALUES (?, ?, ?, ?, ?)",
        (project_id, "Export Test", now, now, "/tmp/x"),
    )


class TestPresets:
    def test_catalog(self, engine: ExportEngine) -> None:
        data = engine.get_available_presets()["data"]
        assert data["count"] == PRESET_COUNT
        assert data["default_preset"] == "youtube_1080p"
        ids = {p["id"] for p in data["presets"]}
        assert ids == {
            "youtube_1080p",
            "youtube_1080p_hq",
            "youtube_4k",
            "fast_preview",
        }
        yt = engine.get_export_preset("youtube_1080p")["data"]["preset"]
        for key in ("crf", "preset", "movflags", "pixel_format", "audio_sample_rate"):
            assert key in yt and str(yt[key]) == str(
                EXPORT_PRESETS["youtube_1080p"][key]
            )

    def test_unknown_preset_defaults(self, engine: ExportEngine) -> None:
        result = engine.get_export_preset("nope")
        assert result["data"]["preset_name"] == "youtube_1080p"
        assert any("Unknown export preset" in w for w in result["warnings"])


class TestSceneRender:
    def test_missing_image(self, engine: ExportEngine, tmp_path: Path) -> None:
        result = engine.render_scene_to_video(
            {"image_path": str(tmp_path / "no.png"), "duration": 8},
            "zoompan",
            "",
            tmp_path / "o.mp4",
        )
        assert (
            result["success"] is False
            and "not found" in (result["error"] or "").lower()
        )

    def test_empty_image_path_never_reaches_ffmpeg(
        self, engine: ExportEngine, tmp_path: Path
    ) -> None:
        # 3.1.0 render blocker: "" became Path("") == "." and FFmpeg
        # died on `-i .` with "Permission denied". Fails early now.
        result = engine.render_scene_to_video(
            {"image_path": "", "image": "", "duration": 8,
             "scene_number": 3},
            "zoompan",
            "",
            tmp_path / "o.mp4",
        )
        assert result["success"] is False
        assert "image path is empty" in (result["error"] or "").lower()

    def test_directory_image_path_rejected(
        self, engine: ExportEngine, tmp_path: Path
    ) -> None:
        # A directory (e.g. ".") satisfies Path.exists() — never a
        # valid image input again.
        result = engine.render_scene_to_video(
            {"image_path": ".", "duration": 8},
            "zoompan",
            "",
            tmp_path / "o.mp4",
        )
        assert result["success"] is False
        assert "not found" in (result["error"] or "").lower()

    def test_image_file_path_key_resolves(
        self, engine: ExportEngine, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The DB column key must be honoured end-to-end (it reaches
        # the ffmpeg check instead of dying on an empty path).
        monkeypatch.setattr(engine.hardware, "find_ffmpeg", lambda: None)
        result = engine.render_scene_to_video(
            {"image_file_path": str(_scene_image(tmp_path)),
             "duration": 8},
            "zoompan",
            "",
            tmp_path / "o.mp4",
        )
        assert "FFmpeg not available" in (result["error"] or "")

    def test_without_ffmpeg(
        self, engine: ExportEngine, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # D4b: simulate absence explicitly — the graceful-failure path
        # must hold even where a real ffmpeg is discoverable.
        monkeypatch.setattr(engine.hardware, "find_ffmpeg", lambda: None)
        result = engine.render_scene_to_video(
            {"image_path": str(_scene_image(tmp_path)), "duration": 8},
            "zoompan",
            "",
            tmp_path / "o.mp4",
        )
        assert result["success"] is False and "FFmpeg not available" in (
            result["error"] or ""
        )

    def test_command_structure(
        self, engine_ff: ExportEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_FRAMES", "1")
        image = _scene_image(tmp_path)
        calls: list[tuple] = []
        result = engine_ff.render_scene_to_video(
            {"image_path": str(image), "duration": 8.0},
            "zoompan=z='1':d=240:s=1920x1080:fps=30",
            "eq=brightness=-0.05:contrast=1.2",
            tmp_path / "seg.mp4",
            progress_callback=lambda p, f, e: calls.append((p, f, e)),
        )
        assert result["success"] is True
        assert result["data"]["total_frames"] == 240
        argv = _argv(tmp_path)
        assert "-loop 1" in argv and "-t 8.000" in argv and "-an" in argv
        assert "zoompan=" in argv and "eq=brightness=-0.05" in argv
        assert "scale=1920:1080" in argv
        assert "-r 30" in argv and "-s 1920x1080" in argv and "-pix_fmt yuv420p" in argv
        assert "-crf 18" in argv and "-preset slow" in argv  # software libx264
        assert (
            calls and calls[-1][0] == 100.0 and calls[-1][1] == 60.0
        )  # progress ended complete
        assert calls[-1][2] == 0.0  # ETA zero at completion

    def test_lut_blend_multi_input(
        self, engine_ff: ExportEngine, tmp_path: Path
    ) -> None:
        lut = tmp_path / "dark_moody.cube"
        lut.write_text("TITLE fake", encoding="utf-8")
        result = engine_ff.render_scene_to_video(
            {"image_path": str(_scene_image(tmp_path)), "duration": 4.0},
            "zoompan=z='1':d=120",
            "eq=brightness=0,lut3d='stale.cube',noise=c0s=4:c0f=t+u",
            tmp_path / "seg.mp4",
            grade_extras={"lut_path": str(lut), "lut_opacity": 0.65},
        )
        assert result["success"] is True
        graph = result["data"]["graph_used"]
        assert (
            "split" in graph
            and "lut3d='" in graph
            and "blend=all_mode=normal:all_opacity=0.65" in graph
        )
        assert (
            graph.count("lut3d") == 1
        )  # stale embedded LUT stripped, single authority
        assert "-filter_complex" in _argv(tmp_path)

    def test_overlay_inputs(self, engine_ff: ExportEngine, tmp_path: Path) -> None:
        dust = tmp_path / "dust.png"
        scratch = tmp_path / "scratch.png"
        Image.new("RGB", (32, 32)).save(dust)
        Image.new("RGB", (32, 32)).save(scratch)
        result = engine_ff.render_scene_to_video(
            {"image_path": str(_scene_image(tmp_path)), "duration": 4.0},
            "",
            "",
            tmp_path / "seg.mp4",
            grade_extras={"dust_overlay": str(dust), "scratch_overlay": str(scratch)},
        )
        assert result["success"] is True
        graph = result["data"]["graph_used"]
        assert (
            "colorchannelmixer=aa=0.35" in graph
            and "colorchannelmixer=aa=0.25" in graph
        )
        assert "overlay=0:0:format=auto" in graph
        argv = _argv(tmp_path)
        assert argv.count("-loop 1 -i") == 3  # image + 2 overlays

    def test_title_card(self, engine_ff: ExportEngine, tmp_path: Path) -> None:
        result = engine_ff.render_title_card(
            "The Black Death: A Complete History of the Plague",
            "documentary",
            4.0,
            tmp_path / "card.mp4",
            preset=None,
        )
        assert result["success"] is True
        card = Path(result["data"]["title_card_image"])
        assert card.exists() and card.suffix == ".png"
        from PIL import Image as PILImage

        with PILImage.open(card) as img:
            assert img.size == (1920, 1080)
        assert "zoompan=" in _argv(tmp_path)


class TestJoin:
    def test_xfade_offsets_and_structure(
        self, engine_ff: ExportEngine, tmp_path: Path
    ) -> None:
        segs = _segments(tmp_path, [8.0, 6.0, 4.0])
        timeline = {
            "transitions": [
                {"type": "crossfade", "duration": 1.0},
                {"type": "dissolve", "duration": 0.8},
            ]
        }
        result = engine_ff.join_segments_with_transitions(
            segs, timeline, tmp_path / "joined.mp4"
        )
        assert result["success"] is True
        argv = _argv(tmp_path)
        assert "xfade=transition=fade:duration=1.000:offset=7.000" in argv
        assert "xfade=transition=dissolve:duration=0.800:offset=12.200" in argv
        assert "scale=1920:1080" in argv and "+faststart" in argv and "-an" in argv
        assert result["data"]["total_duration"] == pytest.approx(16.2, abs=0.01)

    def test_missing_segment(self, engine_ff: ExportEngine, tmp_path: Path) -> None:
        segs = _segments(tmp_path, [8.0], make_files=False)
        result = engine_ff.join_segments_with_transitions(
            segs, {}, tmp_path / "joined.mp4"
        )
        assert (
            result["success"] is False
            and "not found" in (result["error"] or "").lower()
        )

    def test_grouping_for_many_segments(
        self, engine_ff: ExportEngine, tmp_path: Path
    ) -> None:
        segs = _segments(tmp_path, [4.0] * 25)
        timeline = {"transitions": [{"type": "crossfade", "duration": 0.5}] * 24}
        result = engine_ff.join_segments_with_transitions(
            segs, timeline, tmp_path / "joined.mp4"
        )
        assert result["success"] is True
        joins = [
            line
            for line in _argv(tmp_path).splitlines()
            if "-filter_complex" in line and "xfade=" in line
        ]
        assert len(joins) >= 4  # 10+10+5 into groups, plus boundary join

    def test_audio_mux_and_silent_default(
        self, engine_ff: ExportEngine, tmp_path: Path
    ) -> None:
        segs = _segments(tmp_path, [8.0, 6.0])
        audio = tmp_path / "mix.wav"
        audio.write_bytes(b"RIFF" + b"\0" * 800)
        muxed = engine_ff.join_segments_with_transitions(
            segs, {"audio_path": str(audio)}, tmp_path / "joined.mp4"
        )
        assert muxed["success"] is True and muxed["data"]["audio_muxed"] is True
        argv = _argv(tmp_path)
        assert (
            "-c:v copy" in argv
            and "-c:a aac" in argv
            and "-b:a 192k" in argv
            and "-shortest" in argv
        )
        silent = engine_ff.join_segments_with_transitions(
            segs, {}, tmp_path / "joined2.mp4"
        )
        assert silent["data"]["audio_muxed"] is False


class TestHardware:
    def test_no_hardware_detected(self, engine_ff: ExportEngine) -> None:
        data = engine_ff.detect_hardware_acceleration(refresh=True)["data"]
        assert (
            data["hardware"] is False
            and data["encoder"] is None
            and data["fallback"] == "software"
        )

    def test_nvenc_detected_and_verified(
        self, engine_ff: ExportEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_NVENC_LISTED", "1")
        data = engine_ff.detect_hardware_acceleration(refresh=True)["data"]
        assert data["hardware"] is True and data["encoder"] == "h264_nvenc"
        assert "h264_nvenc" in data["tested_candidates"]

    def test_nvenc_broken_falls_back_to_software(
        self, engine_ff: ExportEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_NVENC_LISTED", "1")
        monkeypatch.setenv("FAKE_NVENC_BROKEN", "1")
        data = engine_ff.detect_hardware_acceleration(refresh=True)["data"]
        assert data["hardware"] is False and data["fallback"] == "software"
        assert (
            "h264_nvenc" in data["tested_candidates"]
        )  # listed but failed tiny encode

    def test_encoder_mapping(self, engine: ExportEngine) -> None:
        hw = {"hardware": True, "encoder": "h264_nvenc"}
        assert (
            engine.get_encoder_for_preset({"video_codec": "libx264"}, hw)["data"][
                "codec"
            ]
            == "h264_nvenc"
        )
        assert (
            engine.get_encoder_for_preset({"video_codec": "libx264"}, None)["data"][
                "codec"
            ]
            == "libx264"
        )
        assert (
            engine.get_encoder_for_preset({"video_codec": "libx265"}, hw)["data"][
                "codec"
            ]
            == "libx265"
        )
        hevc = {"hardware": True, "encoder": "hevc_nvenc"}
        assert (
            engine.get_encoder_for_preset({"video_codec": "libx265"}, hevc)["data"][
                "codec"
            ]
            == "hevc_nvenc"
        )


class TestProgressAndEstimates:
    def test_monitor_progress_parsing(self, engine: ExportEngine) -> None:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('frame=  30 fps= 25.0\\rframe= 100 fps= 50.0\\nframe= 300 fps= 50.0\\n'); sys.stderr.flush()",
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        calls: list[tuple] = []
        engine.monitor_ffmpeg_progress(
            proc, 300, lambda p, f, e: calls.append((p, f, e))
        )
        proc.wait(timeout=15)
        assert [c[0] for c in calls] == [10.0, pytest.approx(33.333, abs=0.01), 100.0]
        assert calls[-1][1] == 50.0 and calls[-1][2] == 0.0
        assert calls[0][2] == pytest.approx(270 / 25.0, abs=0.01)

    def test_progress_events_published(
        self, engine_ff: ExportEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_FRAMES", "1")
        events: list[dict] = []
        engine_ff.event_bus.subscribe(
            "render.progress", lambda data: events.append(data)
        )
        result = engine_ff.render_title_card(
            "Bus event test",
            "doc",
            2.0,
            tmp_path / "card.mp4",
            progress_callback=lambda p, f, e: None,
        )
        assert result["success"] is True
        assert any("percent" in e for e in events)

    def test_estimate_render_time(self, engine: ExportEngine) -> None:
        timeline = {"total_duration": 120.0}
        sw = engine.estimate_render_time(
            timeline, "youtube_1080p", hardware_available=False
        )["data"]
        assert sw["total_frames"] == 3600 and sw["estimated_seconds"] == 241
        hw = engine.estimate_render_time(
            timeline, "youtube_1080p", hardware_available=True
        )["data"]
        assert hw["estimated_seconds"] == 61

    def test_estimate_output_size(self, engine: ExportEngine) -> None:
        data = engine.estimate_output_size({"total_duration": 120.0}, "youtube_1080p")[
            "data"
        ]
        assert data["size_bytes"] == int(8192 * 1000 * 120 / 8)
        assert data["size_human"] == "117.2 MB"
        assert data["total_bitrate_kbps"] == 8192


class TestVerifyOutput:
    def test_valid_output(
        self, engine_ff: ExportEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_PROBE_DURATION", "8.5")
        out = tmp_path / "final.mp4"
        out.write_bytes(b"MP4DATA" * 500)
        result = engine_ff.verify_output(out, 8.0)
        assert result["data"]["valid"] is True
        assert result["data"]["has_video"] and result["data"]["has_audio"]
        assert result["data"]["actual_duration"] == 8.5

    def test_duration_mismatch(
        self, engine_ff: ExportEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_PROBE_DURATION", "6.0")
        out = tmp_path / "final.mp4"
        out.write_bytes(b"MP4DATA" * 500)
        result = engine_ff.verify_output(out, 8.0)
        assert result["data"]["valid"] is False
        assert any("Duration mismatch" in i for i in result["data"]["issues"])

    def test_missing_and_empty(
        self, engine_ff: ExportEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = engine_ff.verify_output(tmp_path / "no.mp4", 8.0)
        assert missing["success"] is False
        empty = tmp_path / "empty.mp4"
        empty.touch()
        monkeypatch.setenv("FAKE_PROBE_FAIL", "1")
        result = engine_ff.verify_output(empty, 8.0)
        assert result["data"]["valid"] is False
        assert "File is empty (0 bytes)" in result["data"]["issues"]


class TestCrashRecovery:
    def test_plan_lifecycle(self, engine: ExportEngine) -> None:
        _seed_project(engine, PROJECT)
        scenes = [{"id": f"s{i}", "duration": 8.0} for i in range(3)]
        plan = engine.create_render_plan(PROJECT, scenes, "/tmp/render_x")
        assert plan["success"] is True and plan["data"]["total_segments"] == 3
        session = plan["data"]["session_id"]

        engine.update_segment_status(PROJECT, "s0", "completed")
        done_two = engine.update_segment_status(PROJECT, "s1", "completed")
        assert done_two["data"]["percent"] == pytest.approx(66.7, abs=0.1)

        state = engine.get_resume_state(PROJECT)["data"]
        assert state["session_id"] == session
        assert state["completed_scenes"] == ["s0", "s1"]
        assert [s["scene_id"] for s in state["pending_scenes"]] == ["s2"]
        assert state["resumable"] is True

        finish = engine.finish_render_state(PROJECT, True)
        assert finish["data"]["stage"] == "completed"
        assert engine.get_resume_state(PROJECT)["data"]["stage"] == "completed"

    def test_failure_marks_and_disk_filtering(
        self, engine: ExportEngine, tmp_path: Path
    ) -> None:
        _seed_project(engine, "proj-crash")
        scenes = [{"id": "a", "duration": 4.0}, {"id": "b", "duration": 4.0}]
        engine.create_render_plan("proj-crash", scenes, tmp_path)
        engine.update_segment_status("proj-crash", "a", "completed")
        # Only segments that exist on disk may be reused on resume.
        on_disk = tmp_path / "segment_0000.mp4"
        on_disk.write_bytes(b"SEG")
        failed = engine.update_segment_status(
            "proj-crash", "b", "failed", error="ffmpeg exploded"
        )
        assert failed["data"]["failed"] == 1
        state = engine.get_resume_state("proj-crash")["data"]
        assert state["failed_scenes"] == ["b"]
        assert state["error_count"] == 1 and state["last_error"] == "ffmpeg exploded"
        assert state["segment_files"] == [str(on_disk)]  # missing one filtered out

    def test_ffmpeg_failure_and_recovery_flow(
        self, engine_ff: ExportEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_project(engine_ff, "proj-fail")
        scenes = [{"id": "s0"}, {"id": "s1"}]
        engine_ff.create_render_plan("proj-fail", scenes, tmp_path)
        monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
        result = engine_ff.render_scene_to_video(
            {"image_path": str(_scene_image(tmp_path)), "duration": 4.0},
            "",
            "",
            tmp_path / "s0.mp4",
        )
        assert result["success"] is False and "code 1" in (result["error"] or "")
        engine_ff.update_segment_status(
            "proj-fail", "s0", "failed", error=result["error"]
        )
        state = engine_ff.get_resume_state("proj-fail")["data"]
        assert [s["scene_id"] for s in state["pending_scenes"]] == ["s0", "s1"]
        engine_ff.finish_render_state("proj-fail", False, error=result["error"])
        assert engine_ff.get_resume_state("proj-fail")["data"]["stage"] == "failed"
        monkeypatch.delenv("FAKE_FFMPEG_FAIL")
        recovered = engine_ff.render_scene_to_video(
            {"image_path": str(_scene_image(tmp_path)), "duration": 4.0},
            "",
            "",
            tmp_path / "s0.mp4",
        )
        assert recovered["success"] is True


class TestDebtAndFlags:
    def test_subtitle_segmentation_windows(self, engine: ExportEngine) -> None:
        words = [
            {
                "word_text": f"w{i}",
                "start_time_ms": i * 1000,
                "end_time_ms": i * 1000 + 800,
            }
            for i in range(70)  # 70 seconds of words
        ]
        data = engine.plan_subtitle_segments(words)["data"]
        assert data["count"] == 3  # 0-30, 30-60, 60-70
        assert data["max_words_per_segment"] <= 30
        starts = [s["start_s"] for s in data["segments"]]
        assert starts == sorted(starts)
        assert data["segments"][0]["words"][0]["word_text"] == "w0"
        assert data["segments"][-1]["words"][-1]["word_text"] == "w69"

    def test_export_engine_is_required(self, engine: ExportEngine) -> None:
        assert engine.is_optional_module() is False
