"""Unit tests for modules.quality_checker.QualityChecker.

All 12 spec checks are covered: green-world readiness, per-issue-severity
paths, auto-fix side effects, quality_check_results persistence, and the
human-readable report. Fake ffmpeg/ffprobe come from the shared
cross-platform doubles (tests/conftest.py); no real FFmpeg required.
"""

from __future__ import annotations

import shutil
from collections import namedtuple
from pathlib import Path
from typing import Optional

import pytest
from PIL import Image

from core.service_container import ServiceContainer
from modules.quality_checker import (
    BASELINE_RENDER_RAM_MB,
    CRITICAL,
    ERROR,
    INFO,
    WARNING,
    MB,
    QualityChecker,
)

PROJECT = "proj-qc-1"
NOW = "2026-07-16 00:00:00"

DiskUsage = namedtuple("DiskUsage", "total used free")
FakeVmem = namedtuple("FakeVmem", "available")


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


@pytest.fixture
def qc(project_root: Path, tmp_path: Path) -> QualityChecker:
    """Checker without a resolvable ffmpeg (sandbox has none)."""
    return QualityChecker(_container(project_root, tmp_path))


@pytest.fixture
def qc_ff(project_root: Path, tmp_path: Path, fake_ffmpeg_factory) -> QualityChecker:
    """Checker wired to the fake ffmpeg binary."""
    fake = fake_ffmpeg_factory(tmp_path, tmp_path / "ffmpeg.log")
    return QualityChecker(_container(project_root, tmp_path, str(fake)))


def _seed_project(qc: QualityChecker, folder: Path, project_id: str = PROJECT) -> str:
    qc.db.db.execute(
        "INSERT INTO projects (id, title, created_at, updated_at,"
        " project_folder_path) VALUES (?, ?, ?, ?, ?)",
        (project_id, "Quality Test Doc", NOW, NOW, str(folder)),
    )
    return project_id


def _seed_scene(
    qc: QualityChecker,
    project_id: str,
    number: int,
    *,
    duration: float = 8.0,
    image_path: Optional[Path] = None,
    matched: int = 1,
    transition_in: str = "crossfade",
    transition_out: str = "crossfade",
    transition_duration: float = 0.8,
    animation: str = "ken_burns",
    intensity: str = "medium",
) -> str:
    scene_id = f"{project_id}-s{number}"
    qc.db.db.execute(
        "INSERT INTO scenes (id, project_id, scene_number, image_file_path,"
        " image_matched, duration, transition_in, transition_out,"
        " transition_duration, animation_type, animation_intensity,"
        " created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scene_id,
            project_id,
            number,
            str(image_path) if image_path else None,
            matched,
            duration,
            transition_in,
            transition_out,
            transition_duration,
            animation,
            intensity,
            NOW,
            NOW,
        ),
    )
    return scene_id


def _make_image(path: Path, size=(1920, 1080)) -> Path:
    Image.new("RGB", size, (40, 40, 80)).save(path)
    return path


def _seed_dialogue(qc: QualityChecker, project_id: str, scene_id: str) -> None:
    qc.db.db.execute(
        "INSERT INTO dialogue_lines (id, project_id, scene_id, line_number,"
        " character_name, text_content, created_at, updated_at)"
        " VALUES (?, ?, ?, 1, 'NARRATOR', 'In the year 1347.', ?, ?)",
        (f"{scene_id}-l1", project_id, scene_id, NOW, NOW),
    )


def _seed_timeline(qc: QualityChecker, project_id: str, duration: float) -> None:
    qc.db.db.execute(
        "INSERT INTO timeline_data (id, project_id, total_duration,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (f"tl_{project_id}", project_id, duration, NOW, NOW),
    )


def _seed_subtitles(
    qc: QualityChecker, project_id: str, file_path: Optional[Path]
) -> None:
    qc.db.db.execute(
        "INSERT INTO subtitle_data (id, project_id, final_file_path,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (
            f"sub_{project_id}",
            project_id,
            str(file_path) if file_path else None,
            NOW,
            NOW,
        ),
    )


def _install_piper(qc: QualityChecker) -> None:
    qc.db.db.execute(
        "UPDATE engine_installations SET is_installed = 1, status = 'installed'"
        " WHERE engine_name = 'piper'"
    )


def _seed_voice_profile(qc: QualityChecker, project_id: str) -> None:
    qc.db.db.execute(
        "INSERT INTO voice_profiles (id, project_id, character_name,"
        " created_at, updated_at) VALUES (?, ?, 'NARRATOR', ?, ?)",
        (f"vp_{project_id}", project_id, NOW, NOW),
    )


def _seed_green_world(qc: QualityChecker, tmp_path: Path) -> str:
    """A fully valid project that should come back render-ready."""
    folder = tmp_path / "proj"
    folder.mkdir(parents=True)
    project_id = _seed_project(qc, folder)
    for i in range(3):
        image = _make_image(folder / f"img{i}.png")
        scene_id = _seed_scene(qc, project_id, i, image_path=image)
        _seed_dialogue(qc, project_id, scene_id)
    _seed_timeline(qc, project_id, 24.0)
    srt = folder / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
    _seed_subtitles(qc, project_id, srt)
    _seed_voice_profile(qc, project_id)
    _install_piper(qc)
    return project_id


def _types(issues):
    return {i["type"] for i in issues}


def _severity(issues, issue_type: str) -> str:
    return next(i["severity"] for i in issues if i["type"] == issue_type)


class TestStructure:
    def test_twelve_checks_in_spec_order(self, qc: QualityChecker) -> None:
        data = qc.get_check_names()["data"]
        assert data["total"] == 12
        assert data["checks"] == [
            "check_ffmpeg",
            "check_project_has_scenes",
            "check_all_images",
            "check_tts_engines",
            "check_voice_profiles",
            "check_disk_space",
            "check_output_folder",
            "check_timeline_duration",
            "check_subtitle_file",
            "check_transitions",
            "check_animations",
            "check_ram_available",
        ]

    def test_project_not_found(self, qc: QualityChecker) -> None:
        result = qc.run_full_check("no-such-project")
        assert result["success"] is False
        assert "not found" in (result["error"] or "").lower()

    def test_counts_sum_to_total(self, qc_ff: QualityChecker, tmp_path: Path) -> None:
        _seed_green_world(qc_ff, tmp_path)
        data = qc_ff.run_full_check(PROJECT)["data"]
        assert data["total_checks"] == 12
        assert data["passed"] + data["failed"] + data["warnings"] == 12

    def test_optional_and_disabled(self, qc: QualityChecker) -> None:
        assert qc.is_optional_module() is True
        qc.set_enabled(False)
        _seed_project(qc, Path("/tmp/whatever"))
        assert qc.run_full_check(PROJECT)["success"] is False
        qc.set_enabled(True)


class TestGreenWorld:
    def test_full_green_world_is_render_ready(
        self, qc_ff: QualityChecker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deterministic RAM regardless of the host machine.
        import psutil

        monkeypatch.setattr(
            psutil, "virtual_memory", lambda: FakeVmem(available=32 * 1024 * MB)
        )
        _seed_green_world(qc_ff, tmp_path)
        result = qc_ff.run_full_check(PROJECT)
        assert result["success"] is True
        data = result["data"]
        assert data["is_render_ready"] is True
        assert data["failed"] == 0
        unresolved = [i for i in data["issues"] if not i.get("fixed")]
        assert CRITICAL not in {i["severity"] for i in unresolved}
        assert ERROR not in {i["severity"] for i in unresolved}
        assert "READY   : YES" in data["report_text"]


class TestFfmpeg:
    def test_ffmpeg_missing_critical(
        self, qc: QualityChecker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(qc.hardware, "find_ffmpeg", lambda: None)
        issues = qc.check_ffmpeg()
        assert _severity(issues, "ffmpeg_not_found") == CRITICAL

    def test_ffmpeg_ok_with_fake(self, qc_ff: QualityChecker) -> None:
        assert qc_ff.check_ffmpeg() == []

    def test_ffmpeg_version_failing(
        self, qc_ff: QualityChecker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_FFMPEG_FAIL", "1")
        issues = qc_ff.check_ffmpeg()
        assert _severity(issues, "ffmpeg_version_failed") == CRITICAL


class TestScenesAndImages:
    def test_no_scenes_critical(self, qc_ff: QualityChecker, tmp_path: Path) -> None:
        _seed_project(qc_ff, tmp_path)
        issues = qc_ff.check_project_has_scenes(PROJECT)
        assert _severity(issues, "no_scenes") == CRITICAL

    def test_image_file_not_found(self, qc_ff: QualityChecker, tmp_path: Path) -> None:
        _seed_project(qc_ff, tmp_path)
        _seed_scene(qc_ff, PROJECT, 0, image_path=tmp_path / "ghost.png")
        issues = qc_ff.check_all_images(PROJECT)
        assert _severity(issues, "image_file_not_found") == ERROR

    def test_image_corrupted(self, qc_ff: QualityChecker, tmp_path: Path) -> None:
        _seed_project(qc_ff, tmp_path)
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"this is not a real png at all")
        _seed_scene(qc_ff, PROJECT, 0, image_path=bad)
        issues = qc_ff.check_all_images(PROJECT)
        assert _severity(issues, "image_corrupted") == ERROR

    def test_image_low_resolution(self, qc_ff: QualityChecker, tmp_path: Path) -> None:
        _seed_project(qc_ff, tmp_path)
        small = _make_image(tmp_path / "small.png", size=(640, 360))
        _seed_scene(qc_ff, PROJECT, 0, image_path=small)
        issues = qc_ff.check_all_images(PROJECT)
        assert _severity(issues, "image_low_resolution") == WARNING

    def test_image_match_autofix(self, qc_ff: QualityChecker, tmp_path: Path) -> None:
        _seed_project(qc_ff, tmp_path)
        real = _make_image(tmp_path / "real.png")
        scene_id = _seed_scene(qc_ff, PROJECT, 0, image_path=real, matched=0)
        data = qc_ff.run_full_check(PROJECT)["data"]
        assert data["auto_fixed"] >= 1
        row = qc_ff.db.db.fetch_one(
            "SELECT image_matched FROM scenes WHERE id = ?", (scene_id,)
        )
        assert row["image_matched"] == 1
        applied = [i for i in data["issues"] if i["type"] == "image_not_matched"]
        assert applied and all(i.get("fixed") for i in applied)


class TestEngineAndVoice:
    def test_no_tts_installed_critical(self, qc_ff: QualityChecker) -> None:
        issues = qc_ff.check_tts_engines(PROJECT)
        assert _severity(issues, "tts_no_installed_engine") == CRITICAL

    def test_tts_installed_ok(self, qc_ff: QualityChecker) -> None:
        _install_piper(qc_ff)
        assert qc_ff.check_tts_engines(PROJECT) == []

    def test_voice_profile_autofix(self, qc_ff: QualityChecker, tmp_path: Path) -> None:
        _seed_project(qc_ff, tmp_path)
        scene_id = _seed_scene(qc_ff, PROJECT, 0)
        _seed_dialogue(qc_ff, PROJECT, scene_id)
        issues = qc_ff.check_voice_profiles(PROJECT)
        assert _severity(issues, "voice_profile_missing") == WARNING
        data = qc_ff.run_full_check(PROJECT)["data"]
        assert data["auto_fixed"] >= 1  # create_voice_profiles ran
        row = qc_ff.db.db.fetch_one(
            "SELECT * FROM voice_profiles WHERE project_id = ?"
            " AND character_name = 'NARRATOR'",
            (PROJECT,),
        )
        assert row is not None and row["is_auto_created"] == 1


class TestFoldersAndSpace:
    def test_output_folder_missing_autofix(
        self, qc_ff: QualityChecker, tmp_path: Path
    ) -> None:
        missing = tmp_path / "never_created"
        _seed_project(qc_ff, missing)
        issues = qc_ff.check_output_folder(PROJECT)
        assert _severity(issues, "output_folder_missing") == ERROR
        qc_ff.run_full_check(PROJECT)
        assert missing.exists() and missing.is_dir()

    def test_disk_space_critical(
        self, qc_ff: QualityChecker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        folder = tmp_path / "proj"
        folder.mkdir()
        _seed_project(qc_ff, folder)
        _seed_scene(qc_ff, PROJECT, 0, duration=60.0)
        _seed_timeline(qc_ff, PROJECT, 60.0)
        monkeypatch.setattr(shutil, "disk_usage", lambda p: DiskUsage(10**9, 10**9, 1))
        issues = qc_ff.check_disk_space(PROJECT)
        assert _severity(issues, "insufficient_disk_space") == CRITICAL

    def test_disk_space_comfortable(
        self, qc_ff: QualityChecker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        folder = tmp_path / "proj"
        folder.mkdir()
        _seed_project(qc_ff, folder)
        _seed_scene(qc_ff, PROJECT, 0, duration=30.0)
        _seed_timeline(qc_ff, PROJECT, 30.0)
        monkeypatch.setattr(
            shutil, "disk_usage", lambda p: DiskUsage(10**12, 0, 10**12)
        )
        assert qc_ff.check_disk_space(PROJECT) == []


class TestTimelineSubtitlesPresets:
    def test_timeline_missing_warning(
        self, qc_ff: QualityChecker, tmp_path: Path
    ) -> None:
        _seed_project(qc_ff, tmp_path)
        _seed_scene(qc_ff, PROJECT, 0)
        issues = qc_ff.check_timeline_duration(PROJECT)
        assert _severity(issues, "timeline_missing") == WARNING

    def test_timeline_zero_duration_error(
        self, qc_ff: QualityChecker, tmp_path: Path
    ) -> None:
        _seed_project(qc_ff, tmp_path)
        _seed_timeline(qc_ff, PROJECT, 0.0)
        issues = qc_ff.check_timeline_duration(PROJECT)
        assert _severity(issues, "timeline_zero_duration") == ERROR

    def test_subtitles_not_generated_warning(
        self, qc_ff: QualityChecker, tmp_path: Path
    ) -> None:
        _seed_project(qc_ff, tmp_path)
        issues = qc_ff.check_subtitle_file(PROJECT)
        assert _severity(issues, "subtitles_not_generated") == WARNING

    def test_subtitle_file_missing_error(
        self, qc_ff: QualityChecker, tmp_path: Path
    ) -> None:
        _seed_project(qc_ff, tmp_path)
        _seed_subtitles(qc_ff, PROJECT, tmp_path / "gone.srt")
        issues = qc_ff.check_subtitle_file(PROJECT)
        assert _severity(issues, "subtitle_file_missing") == ERROR

    def test_subtitles_disabled_info(
        self, qc_ff: QualityChecker, tmp_path: Path
    ) -> None:
        _seed_project(qc_ff, tmp_path)
        qc_ff.db.db.execute(
            "UPDATE projects SET has_subtitles = 0 WHERE id = ?", (PROJECT,)
        )
        issues = qc_ff.check_subtitle_file(PROJECT)
        assert _severity(issues, "subtitles_disabled") == INFO

    def test_unknown_transition_and_bad_duration(
        self, qc_ff: QualityChecker, tmp_path: Path
    ) -> None:
        _seed_project(qc_ff, tmp_path)
        _seed_scene(
            qc_ff,
            PROJECT,
            0,
            duration=2.0,
            transition_in="spiral_collapse",
            transition_duration=9.9,
        )
        issues = qc_ff.check_transitions(PROJECT)
        assert _severity(issues, "transition_unknown") == ERROR
        assert _severity(issues, "transition_exceeds_scene") == WARNING

    def test_unknown_animation_and_intensity(
        self, qc_ff: QualityChecker, tmp_path: Path
    ) -> None:
        _seed_project(qc_ff, tmp_path)
        _seed_scene(qc_ff, PROJECT, 0, animation="hyperzoom_spin", intensity="insane")
        issues = qc_ff.check_animations(PROJECT)
        assert _severity(issues, "animation_unknown") == ERROR
        assert _severity(issues, "animation_intensity_unknown") == WARNING

    def test_ram_critical(
        self, qc_ff: QualityChecker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import psutil

        monkeypatch.setattr(
            psutil, "virtual_memory", lambda: FakeVmem(available=256 * MB)
        )
        issues = qc_ff.check_ram_available(PROJECT)
        assert _severity(issues, "insufficient_ram") == CRITICAL

    def test_ram_warn_and_ok(
        self, qc_ff: QualityChecker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import psutil

        _install_piper(qc_ff)
        need = BASELINE_RENDER_RAM_MB + 512
        monkeypatch.setattr(
            psutil, "virtual_memory", lambda: FakeVmem(available=int(need * 1.5) * MB)
        )
        assert _severity(qc_ff.check_ram_available(PROJECT), "low_ram") == WARNING
        monkeypatch.setattr(
            psutil, "virtual_memory", lambda: FakeVmem(available=32 * 1024 * MB)
        )
        assert qc_ff.check_ram_available(PROJECT) == []


class TestPersistenceAndReport:
    def test_results_saved_and_report_content(
        self, qc_ff: QualityChecker, tmp_path: Path
    ) -> None:
        _seed_green_world(qc_ff, tmp_path)
        # Plant one unresolved problem: a missing scene image.
        _seed_scene(qc_ff, PROJECT, 9, image_path=tmp_path / "ghost.png")
        data = qc_ff.run_full_check(PROJECT)["data"]

        row = qc_ff.db.db.fetch_one(
            "SELECT * FROM quality_check_results WHERE project_id = ? ORDER BY"
            " rowid DESC LIMIT 1",
            (PROJECT,),
        )
        assert row is not None
        assert row["total_checks"] == 12
        assert row["is_render_ready"] == 0  # the missing image is an ERROR
        import json

        saved_issues = json.loads(row["issues_json"])
        assert "image_file_not_found" in _types(saved_issues)
        report = data["report_text"]
        for marker in (
            "AUTOPILOT PRE-RENDER QUALITY REPORT",
            "Quality Test Doc",
            "SUMMARY",
            "ISSUES:",
            "AUTO-FIXES APPLIED:",
            "EXPORT SETTINGS:",
            "ESTIMATES:",
        ):
            assert marker in report
        assert "[ERROR   ] (check_all_images)" in report
