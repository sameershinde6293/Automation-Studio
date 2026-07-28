"""Quality checker: pre-render validation and auto-fix (MODULE 15).

Optional BaseModule (CAN BE DISABLED: YES; registry priority 16). Runs 12
checks over a project before rendering, applies safe auto-fixes, persists
results to the quality_check_results table, and builds a human-readable
pre-render report.

Spec source: modules_specification.txt MODULE 15 QUALITY CHECKER. File 11
defines no quality_checker config, so thresholds are documented constants
here. RULE 1: sibling modules are never imported — TTS/transition/
animation/subtitle state is read from the shared DB tables and configs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PIL import Image

from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

MODULE_NAME = "quality_checker"

# Issue severity levels (File 07 MODULE 15)
CRITICAL = "critical"  # render cannot proceed
ERROR = "error"  # render will likely fail
WARNING = "warning"  # render can proceed but output may be affected
INFO = "info"  # informational only

# Thresholds (no File 11 config exists for quality_checker)
MIN_IMAGE_WIDTH = 1280
MIN_IMAGE_HEIGHT = 720
DISK_SAFETY_FACTOR = 1.5  # spec: video file size x 1.5 safety
DISK_WARN_FACTOR = 2.0  # warn when free space is below required x 2
TEMP_MB_PER_MINUTE = 200  # spec: temp files per render minute
TTS_MB_PER_SCENE = 5  # spec: TTS audio estimate per scene
SHORT_VIDEO_SECONDS = 30.0
TIMELINE_MISMATCH_TOLERANCE = 0.10  # 10% scenes-vs-timeline drift
BASELINE_RENDER_RAM_MB = 2048  # render pipeline baseline
RAM_WARN_MULTIPLIER = 2.0
MB = 1024 * 1024

# Transition tokens meaning "no xfade needed".
NO_TRANSITION_TOKENS = {"", "none", "hard_cut"}
VALID_INTENSITIES = {"subtle", "medium", "dramatic"}

_FFMPEG_VERSION_TIMEOUT = 15
_MAX_ISSUES_PER_TYPE = 5


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def _issue(
    issue_type: str,
    severity: str,
    check: str,
    message: str,
    auto_fix: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one issue record (serializable; persisted into issues_json)."""
    return {
        "type": issue_type,
        "severity": severity,
        "check": check,
        "message": message,
        "auto_fix": auto_fix,
        "fixed": False,
    }


def _parse_bitrate_kbps(raw: Any) -> int:
    """Parse preset bitrate strings like '8000k' / '192k' into kbps."""
    text = str(raw or "0").strip().lower().rstrip("k")
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


class QualityChecker(BaseModule):
    """Validate all inputs before render and auto-fix where possible."""

    def __init__(self, container: ServiceContainer) -> None:
        """Initialize checker and load validation catalogs."""
        super().__init__(container, MODULE_NAME)
        self._export_presets: Dict[str, Dict[str, Any]] = {}
        self._transition_ids: set = set()
        self._animation_ids: set = set()
        self._subtitle_style_ids: set = set()
        self._default_export_preset = "youtube_1080p"
        self._run_cache: Optional[Dict[str, Any]] = None
        self._load_catalogs()
        # Auto-fix dispatch table (auto_fix key -> fix function).
        self._fix_functions: Dict[str, Callable[[str], str]] = {
            "create_output_folder": self._fix_create_output_folder,
            "match_existing_images": self._fix_match_existing_images,
            "create_voice_profiles": self._fix_create_voice_profiles,
        }

    def is_optional_module(self) -> bool:
        """Quality checks are skippable by design (CAN BE DISABLED: YES)."""
        return True

    # ------------------------------------------------------------------
    # Catalog loading (RULE 8: configs validated on load)
    # ------------------------------------------------------------------
    def _load_catalogs(self) -> None:
        """Load export/transition/animation/subtitle catalogs defensively."""
        try:
            data = self.config.get_config("export_presets") or {}
            for preset in data.get("presets", []):
                if preset.get("id"):
                    self._export_presets[preset["id"]] = preset
            self._default_export_preset = data.get(
                "default_preset", self._default_export_preset
            )
        except (OSError, ValueError, KeyError) as exc:
            self.log.warning("export_presets config unavailable: %s", exc)
        try:
            data = self.config.get_config("transition_presets") or {}
            self._transition_ids = {
                p["id"] for p in data.get("presets", []) if p.get("id")
            }
        except (OSError, ValueError, KeyError) as exc:
            self.log.warning("transition_presets config unavailable: %s", exc)
        try:
            data = self.config.get_config("animation_presets") or {}
            self._animation_ids = {
                p["id"] for p in data.get("animations", []) if p.get("id")
            }
        except (OSError, ValueError, KeyError) as exc:
            self.log.warning("animation_presets config unavailable: %s", exc)
        try:
            data = self.config.get_config("subtitle_style_presets") or {}
            self._subtitle_style_ids = {
                p["id"] for p in data.get("styles", []) if p.get("id")
            }
        except (OSError, ValueError, KeyError) as exc:
            self.log.warning("subtitle_style_presets config unavailable: %s", exc)

    # ------------------------------------------------------------------
    # DB helpers (PHASE 10: per-run caching)
    # ------------------------------------------------------------------
    def _fetch_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        if self._run_cache is not None and "project" in self._run_cache:
            return self._run_cache["project"]
        row = self.db.db.fetch_one(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        if self._run_cache is not None:
            self._run_cache["project"] = row
        return row

    def _fetch_scenes(self, project_id: str) -> List[Dict[str, Any]]:
        if self._run_cache is not None and "scenes" in self._run_cache:
            return self._run_cache["scenes"]
        rows = self.db.db.fetch_all(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY scene_number",
            (project_id,),
        )
        if self._run_cache is not None:
            self._run_cache["scenes"] = rows
        return rows

    def _fetch_timeline(self, project_id: str) -> Optional[Dict[str, Any]]:
        if self._run_cache is not None and "timeline" in self._run_cache:
            return self._run_cache["timeline"]
        row = self.db.db.fetch_one(
            "SELECT * FROM timeline_data WHERE project_id = ?", (project_id,)
        )
        if self._run_cache is not None:
            self._run_cache["timeline"] = row
        return row

    def _fetch_voice_profiles(self, project_id: str) -> List[Dict[str, Any]]:
        if self._run_cache is not None and "voice_profiles" in self._run_cache:
            return self._run_cache["voice_profiles"]
        rows = self.db.db.fetch_all(
            "SELECT * FROM voice_profiles WHERE project_id = ?",
            (project_id,),
        )
        if self._run_cache is not None:
            self._run_cache["voice_profiles"] = rows
        return rows

    def _fetch_characters(self, project_id: str) -> List[Dict[str, Any]]:
        if self._run_cache is not None and "characters" in self._run_cache:
            return self._run_cache["characters"]
        rows = self.db.db.fetch_all(
            "SELECT DISTINCT character_name FROM dialogue_lines"
            " WHERE project_id = ? AND character_name IS NOT NULL",
            (project_id,),
        )
        if self._run_cache is not None:
            self._run_cache["characters"] = rows
        return rows

    def _fetch_subtitle(self, project_id: str) -> Optional[Dict[str, Any]]:
        if self._run_cache is not None and "subtitle" in self._run_cache:
            return self._run_cache["subtitle"]
        row = self.db.db.fetch_one(
            "SELECT * FROM subtitle_data WHERE project_id = ?", (project_id,)
        )
        if self._run_cache is not None:
            self._run_cache["subtitle"] = row
        return row

    def _fetch_engines(self) -> List[Dict[str, Any]]:
        if self._run_cache is not None and "engines" in self._run_cache:
            return self._run_cache["engines"]
        rows = self.db.db.fetch_all("SELECT * FROM engine_installations")
        if self._run_cache is not None:
            self._run_cache["engines"] = rows
        return rows

    def _project_duration(self, project_id: str) -> float:
        """Timeline duration with a scenes-sum fallback."""
        if self._run_cache is not None and "duration" in self._run_cache:
            return float(self._run_cache["duration"])
        row = self._fetch_timeline(project_id)
        if row and (row.get("total_duration") or 0) > 0:
            dur = float(row["total_duration"])
        else:
            scenes = self._fetch_scenes(project_id)
            dur = float(sum(s.get("duration") or 0.0 for s in scenes))
        if self._run_cache is not None:
            self._run_cache["duration"] = dur
        return dur

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_check_names(self) -> Dict[str, Any]:
        """List the checks run_full_check executes (spec order)."""
        started = time.perf_counter()
        names = [name for name, _ in self._checks("x")]
        return self.make_response(
            True,
            {"checks": names, "total": len(names)},
            duration_ms=_ms(started),
        )

    def run_full_check(self, project_id: str) -> Dict[str, Any]:
        """Run all quality checks for a project, auto-fix, persist, report.

        Returns:
            Response with total_checks, passed, failed, warnings,
            auto_fixed, is_render_ready, issues, report_text (spec shape).
        PHASE 10: caches project/scenes/timeline/profiles during a single run
        to eliminate redundant database queries.
        """
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="quality_checker is disabled")

        self._run_cache = {}
        try:
            project = self._fetch_project(project_id)
            if project is None:
                return self.make_response(
                    False, error=f"Project not found: {project_id}"
                )

            issues: List[Dict[str, Any]] = []
            checks = self._checks(project_id)
            for _name, check_fn in checks:
                try:
                    issues.extend(check_fn())
                except Exception as exc:  # a broken check must not abort others
                    self.log.error("quality check raised: %s", exc, exc_info=True)
                    issues.append(
                        _issue(
                            "check_crashed", ERROR, _name, f"{_name} crashed: {exc}"
                        )
                    )

            auto_fixes = self.auto_fix_issues(issues, project_id)
            # Auto-fixes may mutate database rows; invalidate affected cache entries
            if auto_fixes and self._run_cache is not None:
                self._run_cache.pop("voice_profiles", None)
                self._run_cache.pop("scenes", None)

            unresolved = [i for i in issues if not i.get("fixed")]
            critical = sum(1 for i in unresolved if i["severity"] == CRITICAL)
            errors = sum(1 for i in unresolved if i["severity"] == ERROR)
            is_render_ready = critical == 0 and errors == 0

            # Per-check rollup: passed (no unresolved issues) / failed (any
            # critical|error) / warning (worst is warning|info).
            by_check: Dict[str, List[Dict[str, Any]]] = {}
            for issue in unresolved:
                by_check.setdefault(issue["check"], []).append(issue)
            passed = failed = warnings_only = 0
            for name, _fn in checks:
                bucket = by_check.get(name, [])
                if not bucket:
                    passed += 1
                elif any(i["severity"] in (CRITICAL, ERROR) for i in bucket):
                    failed += 1
                else:
                    warnings_only += 1

            total_checks = len(checks)
            auto_fixed = len(auto_fixes)
            timestamp = utc_now_str()
            summary = {
                "total_checks": total_checks,
                "passed": passed,
                "failed": failed,
                "warnings": warnings_only,
                "auto_fixed": auto_fixed,
                "is_render_ready": is_render_ready,
            }
            self._save_results(project_id, timestamp, summary, issues)

            results_for_report = {
                "project": project,
                "timestamp": timestamp,
                "issues": issues,
                "unresolved": unresolved,
                "auto_fixes": auto_fixes,
                "duration": self._project_duration(project_id),
                **summary,
                "severity_counts": {
                    CRITICAL: critical,
                    ERROR: errors,
                    WARNING: sum(1 for i in unresolved if i["severity"] == WARNING),
                    INFO: sum(1 for i in unresolved if i["severity"] == INFO),
                },
            }
            report_text = self.generate_report(results_for_report)

            return self.make_response(
                True,
                {**summary, "issues": issues, "report_text": report_text},
                duration_ms=_ms(started),
            )
        finally:
            self._run_cache = None

    def _checks(self, project_id: str) -> List[tuple]:
        """The 12 spec checks as (name, zero-arg callable) pairs."""
        return [
            ("check_ffmpeg", lambda: self.check_ffmpeg(project_id)),
            (
                "check_project_has_scenes",
                lambda: self.check_project_has_scenes(project_id),
            ),
            ("check_all_images", lambda: self.check_all_images(project_id)),
            ("check_tts_engines", lambda: self.check_tts_engines(project_id)),
            (
                "check_voice_profiles",
                lambda: self.check_voice_profiles(project_id),
            ),
            ("check_disk_space", lambda: self.check_disk_space(project_id)),
            ("check_output_folder", lambda: self.check_output_folder(project_id)),
            (
                "check_timeline_duration",
                lambda: self.check_timeline_duration(project_id),
            ),
            ("check_subtitle_file", lambda: self.check_subtitle_file(project_id)),
            ("check_transitions", lambda: self.check_transitions(project_id)),
            ("check_animations", lambda: self.check_animations(project_id)),
            ("check_ram_available", lambda: self.check_ram_available(project_id)),
        ]

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------
    def check_ffmpeg(self, project_id: str = "") -> List[Dict[str, Any]]:
        """Verify FFmpeg is available and answers -version."""
        ffmpeg = self.hardware.find_ffmpeg()
        if ffmpeg is None:
            return [
                _issue(
                    "ffmpeg_not_found",
                    CRITICAL,
                    "check_ffmpeg",
                    "FFmpeg not available (engines/ffmpeg/ and PATH searched)",
                )
            ]
        command = [str(ffmpeg), "-version"]
        self.log.info("FFmpeg command: %s", command)  # RULE 4
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=_FFMPEG_VERSION_TIMEOUT
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [
                _issue(
                    "ffmpeg_version_failed",
                    CRITICAL,
                    "check_ffmpeg",
                    f"ffmpeg -version failed to run: {exc}",
                )
            ]
        if result.returncode != 0:
            return [
                _issue(
                    "ffmpeg_version_failed",
                    CRITICAL,
                    "check_ffmpeg",
                    f"ffmpeg -version exited with code {result.returncode}",
                )
            ]
        return []

    def check_project_has_scenes(self, project_id: str) -> List[Dict[str, Any]]:
        """Project must contain at least one scene."""
        scenes = self._fetch_scenes(project_id)
        if not scenes:
            return [
                _issue(
                    "no_scenes",
                    CRITICAL,
                    "check_project_has_scenes",
                    "Project has no scenes to render",
                )
            ]
        return []

    def check_all_images(self, project_id: str) -> List[Dict[str, Any]]:
        """Every scene image must exist, open with Pillow, and be big enough."""
        issues: List[Dict[str, Any]] = []
        for scene in self._fetch_scenes(project_id):
            label = f"scene {scene.get('scene_number')}"
            path_raw = scene.get("image_file_path")
            if not scene.get("image_matched"):
                issues.append(
                    _issue(
                        "image_not_matched",
                        WARNING,
                        "check_all_images",
                        f"{label}: no image matched yet",
                        auto_fix="match_existing_images",
                    )
                )
            if not path_raw:
                if scene.get("image_matched"):
                    issues.append(
                        _issue(
                            "image_path_missing",
                            ERROR,
                            "check_all_images",
                            f"{label}: marked matched but has no image file path",
                        )
                    )
                continue
            path = Path(str(path_raw))
            if not path.exists():
                issues.append(
                    _issue(
                        "image_file_not_found",
                        ERROR,
                        "check_all_images",
                        f"{label}: image file not found: {path_raw}",
                    )
                )
                continue
            try:
                with Image.open(path) as probe:
                    probe.verify()
                with Image.open(path) as image:
                    width, height = image.size
            except Exception as exc:
                issues.append(
                    _issue(
                        "image_corrupted",
                        ERROR,
                        "check_all_images",
                        f"{label}: image cannot be opened ({exc.__class__.__name__})",
                    )
                )
                continue
            if width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT:
                issues.append(
                    _issue(
                        "image_low_resolution",
                        WARNING,
                        "check_all_images",
                        f"{label}: {width}x{height} below"
                        f" {MIN_IMAGE_WIDTH}x{MIN_IMAGE_HEIGHT}",
                    )
                )
        return issues

    def check_tts_engines(self, project_id: str) -> List[Dict[str, Any]]:
        """At least one enabled TTS engine must be installed."""
        rows = self._fetch_engines()
        enabled = [r for r in rows if r.get("is_enabled")]
        installed = [
            r
            for r in enabled
            if r.get("is_installed") or r.get("status") == "installed"
        ]
        if not enabled:
            return [
                _issue(
                    "tts_no_engines_enabled",
                    CRITICAL,
                    "check_tts_engines",
                    "All TTS engines are disabled",
                )
            ]
        if not installed:
            return [
                _issue(
                    "tts_no_installed_engine",
                    CRITICAL,
                    "check_tts_engines",
                    "No enabled TTS engine is installed"
                    " (narration cannot be generated)",
                )
            ]
        issues = []
        broken = [r["engine_name"] for r in enabled if r.get("status") == "error"]
        if broken:
            issues.append(
                _issue(
                    "tts_engine_error",
                    WARNING,
                    "check_tts_engines",
                    f"TTS engines with error status: {', '.join(broken)}",
                )
            )
        return issues

    def check_voice_profiles(self, project_id: str) -> List[Dict[str, Any]]:
        """Dialogue characters should have a voice profile."""
        lines = self._fetch_characters(project_id)
        characters = {str(r["character_name"]) for r in lines}
        if not characters:
            return [
                _issue(
                    "no_dialogue_characters",
                    WARNING,
                    "check_voice_profiles",
                    "No dialogue lines parsed for this project yet",
                )
            ]
        profiles = self._fetch_voice_profiles(project_id)
        known = set()
        for profile in profiles:
            known.add(str(profile.get("character_name") or "").lower())
            aliases = str(profile.get("character_aliases") or "")
            known.update(a.strip().lower() for a in aliases.split(",") if a.strip())
        issues = []
        for character in sorted(characters)[:_MAX_ISSUES_PER_TYPE]:
            if character.lower() not in known:
                issues.append(
                    _issue(
                        "voice_profile_missing",
                        WARNING,
                        "check_voice_profiles",
                        f"Character '{character}' has no voice profile"
                        " (default voice would be used)",
                        auto_fix="create_voice_profiles",
                    )
                )
        return issues

    def check_disk_space(self, project_id: str) -> List[Dict[str, Any]]:
        """Required = video x1.5 safety + temp per minute + TTS per scene."""
        project = self._fetch_project(project_id) or {}
        duration = self._project_duration(project_id)
        if duration <= 0:
            return []  # timeline check reports the zero duration
        scene_count = len(self._fetch_scenes(project_id))

        preset = self._export_presets.get(
            str(project.get("export_preset") or ""), {}
        ) or self._export_presets.get(self._default_export_preset, {})
        total_kbps = _parse_bitrate_kbps(preset.get("video_bitrate"))
        total_kbps += _parse_bitrate_kbps(preset.get("audio_bitrate"))

        video_bytes = total_kbps * 1000 * duration / 8 * DISK_SAFETY_FACTOR
        temp_bytes = (duration / 60.0) * TEMP_MB_PER_MINUTE * MB
        tts_bytes = scene_count * TTS_MB_PER_SCENE * MB
        required = video_bytes + temp_bytes + tts_bytes

        folder = str(project.get("project_folder_path") or "")
        target = folder if folder and Path(folder).exists() else str(Path.cwd())
        try:
            free = shutil.disk_usage(target).free
        except OSError:
            return []  # output folder check reports missing folders
        required_gb = required / 1e9
        free_gb = free / 1e9
        if free < required:
            return [
                _issue(
                    "insufficient_disk_space",
                    CRITICAL,
                    "check_disk_space",
                    f"Need {required_gb:.2f} GB for render, only"
                    f" {free_gb:.2f} GB free on output drive",
                )
            ]
        if free < required * DISK_WARN_FACTOR:
            return [
                _issue(
                    "low_disk_space",
                    WARNING,
                    "check_disk_space",
                    f"Tight disk space: need {required_gb:.2f} GB,"
                    f" {free_gb:.2f} GB free",
                )
            ]
        return []

    def check_output_folder(self, project_id: str) -> List[Dict[str, Any]]:
        """Project folder must exist and be writable (auto-fix creates it)."""
        project = self._fetch_project(project_id) or {}
        folder = str(project.get("project_folder_path") or "").strip()
        if not folder:
            return [
                _issue(
                    "output_folder_unset",
                    ERROR,
                    "check_output_folder",
                    "Project has no project_folder_path",
                )
            ]
        path = Path(folder)
        if not path.exists():
            return [
                _issue(
                    "output_folder_missing",
                    ERROR,
                    "check_output_folder",
                    f"Project folder does not exist: {folder}",
                    auto_fix="create_output_folder",
                )
            ]
        if not path.is_dir() or not os.access(path, os.W_OK):
            return [
                _issue(
                    "output_folder_not_writable",
                    ERROR,
                    "check_output_folder",
                    f"Project folder is not writable: {folder}",
                )
            ]
        return []

    def check_timeline_duration(self, project_id: str) -> List[Dict[str, Any]]:
        """Timeline must exist, be non-zero, and roughly match scene sum."""
        row = self._fetch_timeline(project_id)
        if row is None:
            return [
                _issue(
                    "timeline_missing",
                    WARNING,
                    "check_timeline_duration",
                    "Timeline has not been built yet",
                )
            ]
        duration = float(row.get("total_duration") or 0.0)
        if duration <= 0:
            return [
                _issue(
                    "timeline_zero_duration",
                    ERROR,
                    "check_timeline_duration",
                    "Timeline total duration is zero",
                )
            ]
        issues = []
        if duration < SHORT_VIDEO_SECONDS:
            issues.append(
                _issue(
                    "timeline_short_video",
                    INFO,
                    "check_timeline_duration",
                    f"Timeline is very short ({duration:.1f}s)",
                )
            )
        scene_sum = sum(
            s.get("duration") or 0.0 for s in self._fetch_scenes(project_id)
        )
        if scene_sum > 0:
            drift = abs(duration - scene_sum) / scene_sum
            if drift > TIMELINE_MISMATCH_TOLERANCE:
                issues.append(
                    _issue(
                        "timeline_scene_mismatch",
                        WARNING,
                        "check_timeline_duration",
                        f"Timeline {duration:.1f}s vs scenes {scene_sum:.1f}s"
                        f" ({drift:.0%} drift)",
                    )
                )
        return issues

    def check_subtitle_file(self, project_id: str) -> List[Dict[str, Any]]:
        """When subtitles are enabled, a generated/imported file must exist."""
        project = self._fetch_project(project_id) or {}
        if not project.get("has_subtitles"):
            return [
                _issue(
                    "subtitles_disabled",
                    INFO,
                    "check_subtitle_file",
                    "Subtitles are disabled for this project",
                )
            ]
        row = self._fetch_subtitle(project_id)
        if row is None:
            return [
                _issue(
                    "subtitles_not_generated",
                    WARNING,
                    "check_subtitle_file",
                    "Subtitles enabled but not generated yet",
                )
            ]
        issues = []
        style = str(row.get("style_preset") or "")
        if style and self._subtitle_style_ids and style not in self._subtitle_style_ids:
            issues.append(
                _issue(
                    "subtitle_style_unknown",
                    ERROR,
                    "check_subtitle_file",
                    f"Unknown subtitle style preset: {style}",
                )
            )
        candidates = [
            row.get("final_file_path"),
            row.get("generated_file_path"),
            row.get("imported_file_path"),
        ]
        chosen = next((p for p in candidates if p), None)
        if chosen and not Path(str(chosen)).exists():
            issues.append(
                _issue(
                    "subtitle_file_missing",
                    ERROR,
                    "check_subtitle_file",
                    f"Subtitle file not found: {chosen}",
                )
            )
        elif not chosen:
            issues.append(
                _issue(
                    "subtitles_no_file",
                    WARNING,
                    "check_subtitle_file",
                    "Subtitle record exists but no file path is set",
                )
            )
        return issues

    def check_transitions(self, project_id: str) -> List[Dict[str, Any]]:
        """Scene transition types must exist in the transition catalog."""
        issues: List[Dict[str, Any]] = []
        unknown = 0
        for scene in self._fetch_scenes(project_id):
            label = f"scene {scene.get('scene_number')}"
            for field in ("transition_in", "transition_out"):
                value = str(scene.get(field) or "").strip().lower()
                if value in NO_TRANSITION_TOKENS:
                    continue
                if self._transition_ids and value not in self._transition_ids:
                    unknown += 1
                    if unknown <= _MAX_ISSUES_PER_TYPE:
                        issues.append(
                            _issue(
                                "transition_unknown",
                                ERROR,
                                "check_transitions",
                                f"{label}: unknown transition '{value}' ({field})",
                            )
                        )
            duration = float(scene.get("transition_duration") or 0.0)
            scene_duration = float(scene.get("duration") or 0.0)
            if duration < 0:
                issues.append(
                    _issue(
                        "transition_bad_duration",
                        WARNING,
                        "check_transitions",
                        f"{label}: negative transition duration {duration}",
                    )
                )
            elif scene_duration > 0 and duration > scene_duration:
                issues.append(
                    _issue(
                        "transition_exceeds_scene",
                        WARNING,
                        "check_transitions",
                        f"{label}: transition {duration:.1f}s longer than"
                        f" scene {scene_duration:.1f}s",
                    )
                )
        return issues

    def check_animations(self, project_id: str) -> List[Dict[str, Any]]:
        """Scene animation types and intensities must be known."""
        issues: List[Dict[str, Any]] = []
        project = self._fetch_project(project_id) or {}
        default_anim = str(project.get("default_animation") or "")
        unknown = 0
        for animation, label in [(default_anim, "project default")]:
            if (
                animation
                and self._animation_ids
                and animation not in self._animation_ids
            ):
                issues.append(
                    _issue(
                        "animation_unknown",
                        ERROR,
                        "check_animations",
                        f"{label}: unknown animation '{animation}'",
                    )
                )
        for scene in self._fetch_scenes(project_id):
            label = f"scene {scene.get('scene_number')}"
            animation = str(scene.get("animation_type") or "")
            if (
                animation
                and self._animation_ids
                and animation not in self._animation_ids
            ):
                unknown += 1
                if unknown <= _MAX_ISSUES_PER_TYPE:
                    issues.append(
                        _issue(
                            "animation_unknown",
                            ERROR,
                            "check_animations",
                            f"{label}: unknown animation '{animation}'",
                        )
                    )
            intensity = str(scene.get("animation_intensity") or "")
            if intensity and intensity not in VALID_INTENSITIES:
                issues.append(
                    _issue(
                        "animation_intensity_unknown",
                        WARNING,
                        "check_animations",
                        f"{label}: unknown intensity '{intensity}'",
                    )
                )
        return issues

    def check_ram_available(self, project_id: str) -> List[Dict[str, Any]]:
        """Free RAM must cover render baseline + largest installed engine."""
        try:
            import psutil

            available_mb = psutil.virtual_memory().available / MB
        except ImportError:
            return []
        rows = self._fetch_engines()
        engines = [
            r
            for r in rows
            if r.get("is_enabled")
            and (r.get("is_installed") or r.get("status") == "installed")
        ]
        engine_need = max([int(r.get("ram_required_mb") or 0) for r in engines] or [0])
        required = BASELINE_RENDER_RAM_MB + engine_need
        if available_mb < required:
            return [
                _issue(
                    "insufficient_ram",
                    CRITICAL,
                    "check_ram_available",
                    f"Need ~{required} MB free RAM, only"
                    f" {available_mb:.0f} MB available",
                )
            ]
        if available_mb < required * RAM_WARN_MULTIPLIER:
            return [
                _issue(
                    "low_ram",
                    WARNING,
                    "check_ram_available",
                    f"Low free RAM: {available_mb:.0f} MB available,"
                    f" ~{required} MB recommended minimum",
                )
            ]
        return []

    # ------------------------------------------------------------------
    # Auto-fixes
    # ------------------------------------------------------------------
    def auto_fix_issues(
        self, issues: List[Dict[str, Any]], project_id: str
    ) -> List[Dict[str, Any]]:
        """Apply every auto-fixable issue's fix function (spec method)."""
        applied: List[Dict[str, Any]] = []
        for issue in issues:
            fix_key = issue.get("auto_fix")
            if not fix_key or issue.get("fixed"):
                continue
            fix_fn = self._fix_functions.get(str(fix_key))
            if fix_fn is None:
                self.log.warning("No auto-fix registered for %s", fix_key)
                continue
            try:
                detail = fix_fn(project_id)
            except Exception as exc:
                self.log.error("auto-fix %s failed: %s", fix_key, exc)
                continue
            issue["fixed"] = True
            applied.append({"type": issue["type"], "fix": fix_key, "detail": detail})
        return applied

    def _fix_create_output_folder(self, project_id: str) -> str:
        """Create the missing project folder."""
        project = self._fetch_project(project_id) or {}
        folder = str(project.get("project_folder_path") or "")
        Path(folder).mkdir(parents=True, exist_ok=True)
        return f"created folder {folder}"

    def _fix_match_existing_images(self, project_id: str) -> str:
        """Flip image_matched=1 when the referenced file exists on disk."""
        fixed = 0
        now = utc_now_str()
        for scene in self._fetch_scenes(project_id):
            if scene.get("image_matched"):
                continue
            path_raw = scene.get("image_file_path")
            if path_raw and Path(str(path_raw)).exists():
                self.db.db.execute(
                    "UPDATE scenes SET image_matched = 1, updated_at = ?"
                    " WHERE id = ?",
                    (now, scene["id"]),
                )
                fixed += 1
        return f"matched {fixed} scene image(s) from existing files"

    def _fix_create_voice_profiles(self, project_id: str) -> str:
        """Auto-create default voice profiles for missing characters."""
        lines = self._fetch_characters(project_id)
        profiles = self._fetch_voice_profiles(project_id)
        known = {str(p.get("character_name") or "").lower() for p in profiles}
        now = utc_now_str()
        created = 0
        for row in lines:
            character = str(row["character_name"])
            if character.lower() in known:
                continue
            self.db.db.execute(
                "INSERT OR IGNORE INTO voice_profiles"
                " (id, project_id, character_name, is_auto_created,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, 1, ?, ?)",
                (f"vp_{uuid.uuid4().hex[:12]}", project_id, character, now, now),
            )
            created += 1
        if created > 0 and self._run_cache is not None:
            self._run_cache.pop("voice_profiles", None)
        return f"created {created} default voice profile(s)"

    # ------------------------------------------------------------------
    # Persistence + report
    # ------------------------------------------------------------------
    def _save_results(
        self,
        project_id: str,
        timestamp: str,
        summary: Dict[str, Any],
        issues: List[Dict[str, Any]],
    ) -> None:
        """Persist the check run into quality_check_results."""
        self.db.db.execute(
            "INSERT INTO quality_check_results"
            " (id, project_id, check_timestamp, total_checks, passed_checks,"
            "  failed_checks, warning_count, auto_fixed_count, issues_json,"
            "  is_render_ready)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"qc_{uuid.uuid4().hex[:12]}",
                project_id,
                timestamp,
                int(summary["total_checks"]),
                int(summary["passed"]),
                int(summary["failed"]),
                int(summary["warnings"]),
                int(summary["auto_fixed"]),
                json.dumps(issues),
                1 if summary["is_render_ready"] else 0,
            ),
        )

    def _estimate_output_mb(self, project: Dict[str, Any], duration: float) -> float:
        """Output size estimate in MB (B.12-consistent bitrate math)."""
        preset = self._export_presets.get(
            str(project.get("export_preset") or ""), {}
        ) or self._export_presets.get(self._default_export_preset, {})
        total_kbps = _parse_bitrate_kbps(preset.get("video_bitrate"))
        total_kbps += _parse_bitrate_kbps(preset.get("audio_bitrate"))
        return total_kbps * 1000 * duration / 8 / 1_000_000

    def generate_report(self, check_results: Dict[str, Any]) -> str:
        """Generate the human-readable pre-render report (spec method)."""
        project = check_results.get("project", {})
        counts = check_results.get("severity_counts", {})
        duration = float(check_results.get("duration") or 0.0)
        total_frames = int(duration * 30)
        render_sw = int(total_frames / 15) + 1  # software ~15 fps (B.12)
        render_hw = int(total_frames / 60) + 1  # hardware ~60 fps (B.12)
        size_mb = self._estimate_output_mb(project, duration)

        lines = [
            "=" * 64,
            "AUTOPILOT PRE-RENDER QUALITY REPORT",
            "=" * 64,
            f"Project : {project.get('title', '?')} ({project.get('id', '?')})",
            f"Date    : {check_results.get('timestamp', '')}",
            "-" * 64,
            f"SUMMARY : {check_results.get('passed', 0)}/"
            f"{check_results.get('total_checks', 0)} checks passed, "
            f"{check_results.get('failed', 0)} failed, "
            f"{check_results.get('warnings', 0)} with warnings",
            f"ISSUES  : {counts.get(CRITICAL, 0)} critical, "
            f"{counts.get(ERROR, 0)} errors, "
            f"{counts.get(WARNING, 0)} warnings, "
            f"{counts.get(INFO, 0)} info",
            f"AUTO-FIX: {check_results.get('auto_fixed', 0)} applied",
            f"READY   : {'YES' if check_results.get('is_render_ready') else 'NO'}",
            "-" * 64,
            "ISSUES:",
        ]
        unresolved = check_results.get("unresolved", [])
        if not unresolved:
            lines.append("  (none)")
        for issue in sorted(
            unresolved,
            key=lambda i: (CRITICAL, ERROR, WARNING, INFO).index(i["severity"]),
        ):
            lines.append(
                f"  [{issue['severity'].upper():8s}] ({issue['check']})"
                f" {issue['message']}"
            )
        lines.append("-" * 64)
        lines.append("AUTO-FIXES APPLIED:")
        fixes = check_results.get("auto_fixes", [])
        if not fixes:
            lines.append("  (none)")
        for fix in fixes:
            lines.append(f"  - {fix['fix']}: {fix['detail']}")
        lines.append("-" * 64)
        lines.append("VOICE ASSIGNMENTS:")
        profiles = sorted(
            self._fetch_voice_profiles(str(project.get("id", ""))),
            key=lambda r: str(r.get("character_name") or ""),
        )
        if not profiles:
            lines.append("  (no voice profiles; engine defaults will be used)")
        for profile in profiles:
            voice = profile.get("voice_model") or "(engine default)"
            lines.append(
                f"  - {profile.get('character_name')}:"
                f" {profile.get('engine')} / {voice}"
            )
        lines.append("-" * 64)
        lines.append("EXPORT SETTINGS:")
        lines.append(f"  preset          : {project.get('export_preset')}")
        lines.append(f"  color grade     : {project.get('color_grade_preset')}")
        lines.append(f"  transition      : {project.get('default_transition')}")
        lines.append(f"  animation       : {project.get('default_animation')}")
        lines.append(f"  subtitle style  : {project.get('default_subtitle_style')}")
        lines.append("-" * 64)
        lines.append("ESTIMATES:")
        lines.append(f"  duration        : {duration:.1f}s")
        lines.append(f"  output size     : ~{size_mb:.1f} MB")
        lines.append(
            f"  render time     : ~{render_sw}s software / ~{render_hw}s hardware"
        )
        lines.append("=" * 64)
        return "\n".join(lines)
