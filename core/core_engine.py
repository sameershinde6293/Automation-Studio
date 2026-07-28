"""Core engine: the application orchestrator that wires every module.

RULE 1 (agents instructions): modules never import each other. This file
is THE documented seam — the core layer imports ``modules/*``, creates
one instance per registry entry in ``config/modules_config.json``
priority order, and runs the v1 documentary pipeline end-to-end:

    license -> parse/persist -> channel profile -> voice profiles ->
    keywords -> quality gate (+ autofix) -> images -> TTS ->
    SFX -> final mix -> SRT -> intro/outro -> timeline ->
    per-scene renders (animation + grade) -> join+audio mux ->
    subtitle burn -> verify -> thumbnails -> drive upload (optional,
    self-skips when disabled/offline) -> project completion

Stage criticality mirrors the registry ``required`` flags plus two
pipeline-level additions documented below (TTS and audio: a video
without narration is not a renderable documentary, even though the
modules themselves may be disabled). Optional-stage failures degrade
to warnings so one bad SFX never kills a 20-minute render.

Coarse state is tracked through core/render_state_machine.py
(LOADING -> VALIDATING -> GENERATING -> PROCESSING -> RENDERING ->
EXPORTING -> COMPLETE); per-render crash recovery stays inside
export_engine's render_progress table - this orchestrator does not
duplicate it (RULE 1's "duplicate tiny logic" ban applies to state too).

PLUGINS (D.8): user-provided Python files in ``plugins/`` (registry:
``config/plugins_config.json``) are loaded from their file path -
importlib.util.spec_from_file_location, so they work identically in dev
and in the frozen onedir exe where ``plugins/`` sits beside the EXE.
Each plugin defines ONE BaseModule subclass with PLUGIN_API = 1 and a
``run(context) -> dict`` method. Plugins are NOT pipeline stages (the
v1 stage order stays fixed and honest); they are standalone commands
via ``main.py plugin <name>`` or CoreEngine.run_plugin(). Loading is as
forgiving as module loading (RULE 7): broken files are recorded in the
report, never raised.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.correlation import CorrelationContext
from core.narration_pacing import (
    LEGACY_PAUSE_SECONDS,
    plan_narration_pauses,
    resolve_pacing_config,
)
from core.narration_prosody import (
    plan_narration_prosody,
    resolve_prosody_config,
)
from core.render_state_machine import RenderState, RenderStateMachine
from core.safe_io import atomic_write_text, ensure_directory, safe_unlink
from core.service_container import BaseModule, ServiceContainer
from core.time_helper import utc_now_str

MODULE_NAME = "core_engine"

# The batch_engine processor seam (D.1): batch_engine takes a callable;
# core_engine supplies it (batch_engine itself imports nothing).

# PHASE 6 (natural pauses & human pacing): the legacy FLAT gap between
# narration lines. Still the exact value used when natural pacing is
# disabled (app_settings.natural_pauses_enabled = false) and the
# fallback whenever planning is unavailable, so Phase 1-5 behavior is
# always one setting away. See core/narration_pacing.py — the single
# source of truth shared by this orchestrator, audio_processor (the
# silence actually written into the WAV) and timeline_engine (scene
# durations), which is what keeps voice, images and subtitles aligned.
_PAUSE_BETWEEN_LINES = LEGACY_PAUSE_SECONDS  # matches audio_processor default

# stage name -> (module name, required) for the fixed v1 pipeline order.
# "required" drives abort-vs-warn; module registry "required" drives
# skip-on-disabled. Two deliberate differences from the registry:
#   tts/audio are registry-optional but pipeline-required (no narration
#   or mixed audio -> nothing worth rendering).
_STAGE_MODULES: List[tuple] = [
    ("license", "core.license_manager", True),
    ("parse", "file_parser", True),
    ("channel_profile", "channel_profile_manager", False),
    ("voice_profiles", "voice_profile_manager", False),
    ("keywords", "keyword_analyzer", False),
    ("quality_gate", "quality_checker", False),
    ("images", "image_processor", True),
    ("tts", "tts_engine_manager", True),
    ("sfx", "sfx_engine", False),
    ("audio_mix", "audio_processor", True),
    ("intro_outro", "intro_outro_engine", False),
    ("subtitles", "subtitle_engine", False),
    ("timeline", "timeline_engine", True),
    ("export", "export_engine", True),
    ("burn_subtitles", "subtitle_engine", False),
    ("verify", "export_engine", True),
    ("thumbnails", "thumbnail_generator", False),
    ("drive_upload", "drive_upload_engine", False),
]
_LICENSE_ALLOWED_STATUSES = ("active", "trial")

# D.8 plugin contract: plugins declare PLUGIN_API with this integer.
PLUGIN_API_VERSION = 1


def _ms(started: float) -> float:
    """Elapsed milliseconds."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def _pick_path(data: Dict[str, Any], *keys: str) -> Optional[str]:
    """First non-empty string value among candidate response keys."""
    for key in keys:
        value = data.get(key)
        if value and isinstance(value, str):
            return value
    return None


class CoreEngine(BaseModule):
    """Create and orchestrate all pipeline module instances."""

    def __init__(
        self,
        container: ServiceContainer,
        module_loader: Optional[Callable[[str], Optional[Any]]] = None,
        auto_load: bool = True,
    ) -> None:
        """Initialize the orchestrator and (optionally) load modules.

        ``module_loader`` is the test/DI seam: when supplied it maps a
        registry module name to an instance, bypassing importlib.
        """
        super().__init__(container, MODULE_NAME)
        self.container = container  # BaseModule keeps services, not this
        self._assets_col_cache: Optional[str] = None  # PRAGMA-resolved
        # PHASE 8: scene_number -> timeline scene lookup (see
        # _timeline_scene_for). Held here, never inside the timeline
        # dict, so nothing new is ever written to timeline_json.
        self._timeline_index_cache: Optional[
            Tuple[Any, int, Dict[int, Dict[str, Any]]]
        ] = None
        self._module_loader = module_loader
        self._modules: Dict[str, Any] = {}
        self._load_report: Dict[str, Dict[str, Any]] = {}
        self._plugins: Dict[str, Any] = {}
        self._plugin_report: Dict[str, Dict[str, Any]] = {}
        self._state_machine = RenderStateMachine(self.event_bus)
        self._running = False
        self._cancel_requested = False
        self._last_pipeline: Optional[Dict[str, Any]] = None
        if auto_load:
            self.load_modules()
            self.load_plugins()

    def is_optional_module(self) -> bool:
        """The orchestrator is required infrastructure."""
        return False

    # ------------------------------------------------------------------
    # Module loading (registry-driven; importlib lives ONLY here)
    # ------------------------------------------------------------------
    def _registry(self) -> List[Dict[str, Any]]:
        try:
            data = self.config.get_config("modules_config") or {}
        except (OSError, ValueError, KeyError) as exc:
            self.log.error("modules_config unreadable: %s", exc)
            return []
        entries = data.get("modules") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        return sorted(entries, key=lambda e: int(e.get("priority") or 99))

    @staticmethod
    def _discover_class(mod: Any, module_name: str) -> Optional[type]:
        """Find the BaseModule subclass defined in the loaded module."""
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                issubclass(obj, BaseModule)
                and obj is not BaseModule
                and obj.__module__ == mod.__name__
            ):
                return obj
        return None

    def load_modules(self) -> Dict[str, Any]:
        """Instantiate every enabled registry module in priority order.

        Import/instantiation failures are recorded per module and never
        raised: the pipeline decides at stage time whether a missing
        module is fatal (required) or a warning (optional) - RULE 7.
        """
        started = time.perf_counter()
        self._load_report = {}
        loaded = 0
        for entry in self._registry():
            name = str(entry.get("name") or "")
            if not name:
                continue
            report = {
                "enabled": bool(entry.get("enabled", True)),
                "required": bool(entry.get("required", False)),
                "priority": int(entry.get("priority") or 99),
                "loaded": False,
                "error": None,
            }
            if not report["enabled"]:
                report["error"] = "disabled in registry"
                self._load_report[name] = report
                continue
            try:
                if self._module_loader is not None:
                    instance = self._module_loader(name)
                    if instance is None:
                        raise ImportError(f"module_loader returned None: {name}")
                else:
                    mod = importlib.import_module(f"modules.{name}")
                    cls = self._discover_class(mod, name)
                    if cls is None:
                        raise ImportError(f"no BaseModule subclass in {name}")
                    instance = cls(self.container)
            except Exception as exc:  # noqa: BLE001 - isolate module faults
                self.log.error("module load failed %s: %s", name, exc)
                report["error"] = str(exc)
                self._load_report[name] = report
                continue
            self._modules[name] = instance
            report["loaded"] = True
            loaded += 1
            self._load_report[name] = report
        return self.make_response(
            True,
            {
                "loaded": loaded,
                "total_registry": len(self._load_report),
                "modules": self._load_report,
            },
            duration_ms=_ms(started),
        )

    def module(self, name: str) -> Optional[Any]:
        """Return a loaded module instance (or None)."""
        return self._modules.get(name)

    @staticmethod
    def stage_names() -> List[str]:
        """Ordered pipeline stage names (UI progress displays, docs).

        Public additive API (D.4): derived from the module-level
        _STAGE_MODULES table so the UI can never drift from the real
        stage plan.
        """
        return [name for name, _module, _required in _STAGE_MODULES]

    def get_module_status(self) -> Dict[str, Any]:
        """Load report for every registry entry."""
        started = time.perf_counter()
        return self.make_response(
            True,
            {
                "loaded_modules": sorted(self._modules),
                "report": self._load_report,
            },
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Plugin interface (D.8): user plugins from plugins/ + registry JSON
    # ------------------------------------------------------------------
    def _plugins_folder(self) -> Path:
        """plugins/ beside the project root (parent of config folder)."""
        folder = getattr(self.config, "config_folder", None)
        try:
            if folder:
                return Path(str(folder)).resolve().parent / "plugins"
        except (TypeError, ValueError, OSError):
            pass
        return Path.cwd() / "plugins"

    def _plugin_registry(self) -> List[Dict[str, Any]]:
        try:
            data = self.config.get_config("plugins_config") or {}
        except (OSError, ValueError, KeyError) as exc:
            self.log.error("plugins_config unreadable: %s", exc)
            return []
        entries = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        clean = []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name"):
                clean.append(entry)
        return sorted(clean, key=lambda e: str(e.get("name")))

    def load_plugins(self) -> Dict[str, Any]:
        """Instantiate every enabled plugin (file-path import).

        Mirrors load_modules' forgiveness contract: every failure is
        recorded in the report (RULE 7), never raised. Plugins load from
        their file path, so no package semantics are needed - this works
        for user files dropped beside the frozen exe too.
        """
        started = time.perf_counter()
        self._plugin_report = {}
        loaded = 0
        folder = self._plugins_folder()
        for entry in self._plugin_registry():
            name = str(entry.get("name"))
            report = {
                "enabled": bool(entry.get("enabled", True)),
                "loaded": False,
                "error": None,
            }
            if not report["enabled"]:
                report["error"] = "disabled in registry"
                self._plugin_report[name] = report
                continue
            path = folder / f"{name}.py"
            if not path.is_file():
                report["error"] = f"plugin file not found: {path}"
                self._plugin_report[name] = report
                continue  # RULE 7: record, never raise
            try:
                spec = importlib.util.spec_from_file_location(
                    f"autopilot_plugin_{name}", path
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                cls = self._discover_class(mod, name)
                if cls is None:
                    raise ImportError(f"no BaseModule subclass in {name}")
                api = getattr(mod, "PLUGIN_API", None)
                if api != PLUGIN_API_VERSION:
                    raise ImportError(
                        f"plugin API {api!r} unsupported"
                        f" (need {PLUGIN_API_VERSION})"
                    )
                try:
                    instance = cls(self.container)
                except TypeError as exc:
                    # Minimal plugins skip __init__ boilerplate entirely;
                    # BaseModule then needs the module_name argument.
                    if "module_name" not in str(exc):
                        raise
                    instance = cls(self.container, name)
            except Exception as exc:  # noqa: BLE001 - isolate plugin faults
                self.log.error("plugin load failed %s: %s", name, exc)
                report["error"] = str(exc)
                self._plugin_report[name] = report
                continue
            self._plugins[name] = instance
            report["loaded"] = True
            loaded += 1
            self._plugin_report[name] = report
        return self.make_response(
            True,
            {"loaded": loaded, "plugins": self._plugin_report},
            duration_ms=_ms(started),
        )

    def plugin(self, name: str) -> Optional[Any]:
        """Return a loaded plugin instance (or None)."""
        return self._plugins.get(name)

    def plugin_names(self) -> List[str]:
        """Sorted names of successfully loaded plugins."""
        return sorted(self._plugins)

    def get_plugin_status(self) -> Dict[str, Any]:
        """Load report for every plugins_config entry."""
        started = time.perf_counter()
        return self.make_response(
            True,
            {
                "loaded_plugins": self.plugin_names(),
                "report": self._plugin_report,
            },
            duration_ms=_ms(started),
        )

    def run_plugin(
        self, name: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Run one plugin's run(context); normalize to a response dict.

        Plugin crashes are isolated into failed responses exactly like
        stage crashes; plugin.started/completed/failed ride the bus.
        """
        started = time.perf_counter()
        instance = self._plugins.get(name)
        if instance is None:
            return self.make_response(
                False, error=f"plugin not loaded: {name}"
            )
        self.event_bus.publish("plugin.started", {"plugin": name})
        try:
            result = instance.run(dict(context or {}))
        except Exception as exc:  # noqa: BLE001 - plugin fault isolation
            self.log.exception("plugin %s crashed", name)
            self.event_bus.publish(
                "plugin.failed", {"plugin": name, "error": str(exc)}
            )
            return self.make_response(
                False, error=f"plugin crash: {exc}",
                duration_ms=_ms(started),
            )
        if not isinstance(result, dict):
            result = {"success": True, "data": {"result": result}}
        data = result.get("data")
        if not isinstance(data, dict):
            data = {
                k: v
                for k, v in result.items()
                if k
                not in (
                    "success", "data", "error", "warnings",
                    "module", "timestamp", "duration_ms",
                )
            }
        success = bool(result.get("success", True))
        response = self.make_response(
            success,
            data=data,
            error=result.get("error"),
            warnings=list(result.get("warnings") or []),
            duration_ms=_ms(started),
        )
        if success:
            self.event_bus.publish("plugin.completed", {"plugin": name})
        else:
            self.event_bus.publish(
                "plugin.failed",
                {"plugin": name, "error": response.get("error")},
            )
        return response

    # ------------------------------------------------------------------
    # Batch wiring (batch_engine RULE 1 seam)
    # ------------------------------------------------------------------
    def make_batch_processor(self) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        """Closure for batch_engine.process_queue(processor=...).

        The batch queue row provides project_id (preferred) or a folder;
        a folder-only queue item fails honestly (parsing project folders
        is the UI/import flow's job, not the render queue's).
        """

        def _processor(item: Dict[str, Any]) -> Dict[str, Any]:
            project_id = item.get("project_id")
            if not project_id:
                return {
                    "success": False,
                    "data": {},
                    "error": "batch item has no project_id",
                }
            channel_profile = item.get("channel_profile_id")
            result = self.run_project_pipeline(
                str(project_id),
                channel_profile_id=channel_profile or None,
            )
            return dict(result)

        return _processor

    # ------------------------------------------------------------------
    # Public pipeline entry points
    # ------------------------------------------------------------------
    def generate_srt_from_script_evenly(
        self, script_path: str, video_path: str
    ) -> Dict[str, Any]:
        """Generate an SRT from a script's text, timed to an existing video.

        FEATURE (v3.2.15): supports the standalone "subtitles only" mode
        (main.py cmd_subtitles_only) — burns captions onto an
        already-finished video with no full render involved.

        HONEST LIMITATION: this has no TTS-generated word timestamps to
        sync against (that's what the full pipeline uses for
        word-accurate timing). Instead, each script line's screen time
        is distributed across the video's real duration (measured via
        ffprobe), proportional to that line's word count — a reasonable
        approximation for narration-paced content, not word-accurate
        sync. If precise sync matters, use the full render pipeline,
        which generates real subtitles from actual TTS timestamps.
        """
        try:
            file_parser = self._modules.get("file_parser")
            if file_parser is None:
                from modules.file_parser import FileParser
                file_parser = FileParser(self.container)
            parsed = file_parser.parse_script(script_path)
            if not parsed.get("success"):
                return {"success": False, "error": parsed.get("error")}
            lines: List[str] = []
            for scene in (parsed.get("data") or {}).get("scenes") or []:
                for d in scene.get("dialogue") or []:
                    text = str(d.get("text") or "").strip()
                    if text:
                        lines.append(text)
            if not lines:
                return {"success": False, "error": "script has no narration text"}

            hardware = self.hardware
            ffprobe = hardware.find_ffprobe() if hardware else None
            if not ffprobe:
                return {"success": False, "error": "ffprobe not found"}
            proc = subprocess.run(
                [str(ffprobe), "-v", "quiet", "-show_entries",
                 "format=duration", "-print_format", "json", str(video_path)],
                capture_output=True, text=True, timeout=30,
            )
            video_duration = float(
                (json.loads(proc.stdout or "{}").get("format") or {}).get(
                    "duration") or 0.0
            )
            if video_duration <= 0:
                return {"success": False, "error": "could not read video duration"}

            word_counts = [max(1, len(line.split())) for line in lines]
            total_words = sum(word_counts)
            srt_lines = []
            t = 0.0
            for index, (line, words) in enumerate(zip(lines, word_counts), start=1):
                span = video_duration * (words / total_words)
                start, end = t, min(video_duration, t + span)
                srt_lines.append(
                    f"{index}\n{self._srt_timestamp(start)} --> "
                    f"{self._srt_timestamp(end)}\n{line}\n"
                )
                t = end
            out_dir = Path(video_path).parent
            srt_path = out_dir / f"{Path(video_path).stem}_generated.srt"
            # PHASE 9: atomic — a partially written sidecar is worse than
            # none at all (a player silently shows truncated captions).
            if not atomic_write_text(srt_path, "\n".join(srt_lines)):
                return {
                    "success": False,
                    "error": f"could not write SRT file: {srt_path}",
                }
            return {"success": True, "data": {"srt_path": str(srt_path)}}
        except Exception as exc:  # noqa: BLE001 - standalone tool, report cleanly
            return {"success": False, "error": str(exc)}

    @staticmethod
    def _srt_timestamp(seconds: float) -> str:
        seconds = max(0.0, seconds)
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int(round((seconds - int(seconds)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def run_script_pipeline(
        self,
        script_path: Any,
        project_folder: Any,
        title: Optional[str] = None,
        images_folder: Optional[Any] = None,
        export_preset: Optional[str] = None,
        channel_profile_id: Optional[str] = None,
        skip_stages: tuple = (),
        enforce_license: bool = True,
        quality_gate: bool = False,
        preview_max_scenes: Optional[int] = None,
        two_pass_export: bool = False,
        target_duration_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """End-to-end run: parse a script file, persist, then render.

        ``quality_gate`` False (default) runs the 12 checks as advisory
        warnings; True aborts the pipeline on unresolved critical/error
        issues (use for unattended batch renders).

        ``preview_max_scenes`` (v3.2.4): when set, only the first N scenes
        are actually rendered (the rest are skipped) and export defaults
        to the low-res "fast_preview" preset unless export_preset is
        explicitly given — lets you sanity-check timing/sync/visuals in
        under a minute instead of waiting for a full multi-hour render.

        ``two_pass_export`` (v3.2.4): opt-in higher quality-per-size final
        encode (roughly 2x slower for the subtitle-burn step specifically);
        intended for a final deliverable render, not quick tests.

        ``target_duration_seconds`` (v3.2.16): fit the narration to a
        target length by adjusting TTS speed — e.g. a naturally 5-minute
        script stretched to fill 10 minutes. Generates narration once at
        normal speed, measures the real result, computes the exact speed
        needed, and regenerates once more at that speed. Clamped to
        0.5x-2.0x (the same range the Speed slider elsewhere in this app
        already allows) — if the target requires going further than
        that, the result reports "achievable": False rather than
        producing distorted, unnatural-sounding speech.
        """
        if self._running:
            return self.make_response(
                False, error="a pipeline run is already in progress"
            )
        ctx: Dict[str, Any] = {
            "script_path": str(script_path),
            "project_folder": str(project_folder),
            "project_title": title,
            "images_folder": str(images_folder) if images_folder else None,
            "export_preset": export_preset or (
                "fast_preview" if preview_max_scenes else None
            ),
            "channel_profile_id": channel_profile_id,
            "parsed_data": None,
            "project_id": None,
            "preview_max_scenes": preview_max_scenes,
            "two_pass_export": two_pass_export,
            "target_duration_seconds": target_duration_seconds,
        }
        return self._run(ctx, skip_stages, enforce_license, quality_gate)

    def run_project_pipeline(
        self,
        project_id: str,
        export_preset: Optional[str] = None,
        channel_profile_id: Optional[str] = None,
        skip_stages: tuple = (),
        enforce_license: bool = True,
        quality_gate: bool = False,
    ) -> Dict[str, Any]:
        """Render a project that already has scenes/dialogue in the DB."""
        if self._running:
            return self.make_response(
                False, error="a pipeline run is already in progress"
            )
        project = self.db.db.fetch_one(
            "SELECT * FROM projects WHERE id = ?", (str(project_id),)
        )
        if project is None:
            return self.make_response(
                False, error=f"Project not found: {project_id}"
            )
        ctx = {
            "script_path": None,
            "project_folder": str(project.get("project_folder_path") or "."),
            "project_title": project.get("title"),
            "images_folder": None,
            "export_preset": export_preset or project.get("export_preset"),
            "channel_profile_id": channel_profile_id
            or project.get("channel_profile_id"),
            "parsed_data": None,
            "project_id": str(project_id),
        }
        return self._run(ctx, skip_stages, enforce_license, quality_gate)

    def cancel_pipeline(self) -> Dict[str, Any]:
        """Best-effort cancel: honored between scenes/lines, not mid-FFmpeg."""
        started = time.perf_counter()
        self._cancel_requested = True
        return self.make_response(
            True, {"cancel_requested": True}, duration_ms=_ms(started)
        )

    def get_state(self) -> Dict[str, Any]:
        """Coarse render-machine state + last pipeline summary."""
        started = time.perf_counter()
        return self.make_response(
            True,
            {
                "render_state": self._state_machine.state_name,
                "pipeline_running": self._running,
                "cancel_requested": self._cancel_requested,
                "last_pipeline": self._last_pipeline,
            },
            duration_ms=_ms(started),
        )

    # ------------------------------------------------------------------
    # Pipeline driver
    # ------------------------------------------------------------------
    def _run(
        self,
        ctx: Dict[str, Any],
        skip_stages: tuple,
        enforce_license: bool,
        quality_gate: bool,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        if not self._enabled:
            return self.make_response(False, error="core_engine is disabled")
        if self._running:
            return self.make_response(
                False, error="a pipeline run is already in progress"
            )
        self._running = True
        self._cancel_requested = False
        self._state_machine.reset_to_idle()
        self._transition(RenderState.LOADING, "pipeline start")
        correlation = CorrelationContext.new_id()
        CorrelationContext.set(correlation)
        stages: List[Dict[str, Any]] = []
        ctx["warnings"] = []
        ctx["skip_stages"] = set(skip_stages or ())
        ctx["quality_gate"] = bool(quality_gate)
        ctx["enforce_license"] = bool(enforce_license)
        ctx["correlation_id"] = correlation
        self.event_bus.publish(
            "pipeline.started",
            {"project_id": ctx.get("project_id"), "correlation_id": correlation},
        )
        failed_stage: Optional[str] = None
        aborted_reason: Optional[str] = None
        try:
            for stage_name, _module, _required in _STAGE_MODULES:
                result = self._execute_stage(stage_name, ctx)
                stages.append(result)
                self._emit_overall_progress(ctx, stage_name, started)
                if (
                    stage_name == "tts"
                    and result["status"] not in ("failed", "cancelled")
                    and ctx.get("target_duration_seconds")
                ):
                    stages.append(self._apply_target_duration(ctx))
                if result["status"] == "failed":
                    failed_stage = stage_name
                    aborted_reason = (
                        f"pipeline aborted at stage '{stage_name}':"
                        f" {result.get('error')}"
                    )
                    self._transition(RenderState.FAILED, f"stage {stage_name}")
                    break
                if result["status"] == "cancelled":
                    failed_stage = stage_name
                    aborted_reason = "pipeline cancelled by user"
                    self._transition(RenderState.CANCELLED, "user request")
                    break
            if failed_stage is None:
                self._finalize_success(ctx)
        finally:
            # PHASE 9 (resource cleanup): the reentrancy guard and the
            # correlation id were already released here on every path;
            # module-held buffers now are too, so a failed or cancelled
            # render doesn't leave decoded audio resident until the next
            # one starts.
            self._release_memory()
            self._running = False
            CorrelationContext.clear()

        summary = {
            "project_id": ctx.get("project_id"),
            "correlation_id": correlation,
            "stages": stages,
            "failed_stage": failed_stage,
            "output_file_path": ctx.get("final_output"),
            "warnings": ctx["warnings"],
            "render_state": self._state_machine.state_name,
            "target_duration_result": ctx.get("target_duration_result"),
        }
        self._last_pipeline = {
            "project_id": ctx.get("project_id"),
            "failed_stage": failed_stage,
            "output_file_path": ctx.get("final_output"),
            "timestamp": utc_now_str(),
        }
        if failed_stage:
            self.event_bus.publish(
                "pipeline.failed",
                {"project_id": ctx.get("project_id"), "stage": failed_stage},
            )
            return self.make_response(
                False,
                data=summary,
                error=aborted_reason
                or f"pipeline aborted at stage '{failed_stage}'",
                warnings=ctx["warnings"],
                duration_ms=_ms(started),
            )
        self.event_bus.publish(
            "pipeline.completed",
            {
                "project_id": ctx.get("project_id"),
                "output_file_path": ctx.get("final_output"),
            },
        )
        return self.make_response(
            True,
            data=summary,
            warnings=ctx["warnings"],
            duration_ms=_ms(started),
        )

    _LEGAL_CHAIN = (
        RenderState.LOADING,
        RenderState.VALIDATING,
        RenderState.GENERATING,
        RenderState.PROCESSING,
        RenderState.RENDERING,
        RenderState.EXPORTING,
        RenderState.COMPLETE,
    )

    def _release_memory(self) -> None:
        """Drop module-held caches and collect, after a MemoryError.

        PHASE 9: the audio module can hold up to 256MB of decoded PCM
        (the PHASE 8 memo). Releasing it gives the failure handler —
        and the rest of the session — room to work. Anything a module
        does not expose is simply skipped; this is best-effort.
        """
        for module in self._modules.values():
            releaser = getattr(module, "_decode_cache_clear", None)
            if callable(releaser):
                try:
                    releaser()
                except Exception:  # noqa: BLE001 - cleanup only
                    pass
        try:
            import gc

            gc.collect()
        except Exception:  # noqa: BLE001 - cleanup only
            pass

    def _transition(self, state: RenderState, reason: str) -> None:
        """Transition the render machine, walking the legal chain on skips.

        Skipped stages may leave the machine several states behind the
        next real stage (e.g. GENERATING -> RENDERING when subtitles are
        skipped). The chain hop keeps the machine honest without
        spamming illegal-transition warnings.
        """
        if self._state_machine.state is state:
            return
        if self._state_machine.transition_to(state, reason):
            return
        chain = list(self._LEGAL_CHAIN)
        if state not in chain or self._state_machine.state not in chain:
            self.log.warning(
                "illegal render transition -> %s (%s)", state.name, reason
            )
            return
        current_idx = chain.index(self._state_machine.state)
        target_idx = chain.index(state)
        for hop in chain[current_idx + 1 : target_idx + 1]:
            self._state_machine.transition_to(hop, f"en route to {state.name}")

    def _execute_stage(
        self, stage_name: str, ctx: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Run one stage; uniform result for the stages list."""
        started = time.perf_counter()
        module_name, required = next(
            (m, r) for s, m, r in _STAGE_MODULES if s == stage_name
        )
        if stage_name in ctx["skip_stages"]:
            self.event_bus.publish(
                "pipeline.stage_skipped",
                {"stage": stage_name, "reason": "skip_stages"},
            )
            return {
                "stage": stage_name, "status": "skipped",
                "reason": "skip_stages", "duration_ms": _ms(started),
            }
        if stage_name == "license" and not ctx.get("enforce_license", True):
            return {
                "stage": stage_name, "status": "skipped",
                "reason": "license enforcement off", "duration_ms": _ms(started),
            }
        instance = None
        if stage_name == "license":
            instance = self._license_instance(ctx)
        else:
            instance = self._modules.get(module_name)
        if instance is None:
            if stage_name == "license":
                msg = "license manager unavailable; continuing without gate"
                ctx["warnings"].append(msg)
                self.log.warning(msg)
                return {
                    "stage": stage_name, "status": "warning", "error": msg,
                    "duration_ms": _ms(started),
                }
            msg = f"module '{module_name}' not loaded"
            if required:
                self.event_bus.publish(
                    "pipeline.stage_failed", {"stage": stage_name, "error": msg}
                )
                return {
                    "stage": stage_name, "status": "failed", "error": msg,
                    "duration_ms": _ms(started),
                }
            ctx["warnings"].append(f"{stage_name} skipped: {msg}")
            return {
                "stage": stage_name, "status": "skipped", "reason": msg,
                "duration_ms": _ms(started),
            }
        enabled = getattr(instance, "enabled", True)
        if not enabled:
            msg = f"module '{module_name}' disabled"
            if required:
                return {
                    "stage": stage_name, "status": "failed", "error": msg,
                    "duration_ms": _ms(started),
                }
            ctx["warnings"].append(f"{stage_name} skipped: {msg}")
            return {
                "stage": stage_name, "status": "skipped", "reason": msg,
                "duration_ms": _ms(started),
            }

        self.event_bus.publish(
            "pipeline.stage_started",
            {"stage": stage_name, "project_id": ctx.get("project_id")},
        )
        handler = getattr(self, f"_stage_{stage_name}")
        try:
            outcome = handler(instance, ctx)
        except MemoryError:
            # PHASE 9 (memory allocation failures): a long render can
            # genuinely exhaust RAM (a multi-hour narration buffer, a
            # huge filtergraph). Release what this process is holding
            # BEFORE building the failure response, so the orchestrator
            # can still report cleanly and the app stays usable instead
            # of dying with an unhandled MemoryError mid-render.
            self._release_memory()
            self.log.error("stage %s ran out of memory", stage_name)
            outcome = {
                "success": False,
                "error": (
                    f"stage '{stage_name}' ran out of memory — try a lower "
                    "export preset, or close other applications"
                ),
            }
        except KeyboardInterrupt:
            # PHASE 9 (safe cancellation): Ctrl-C during a stage is a
            # cancel, not a crash — mark it so the pipeline unwinds
            # through its normal cancelled path and leaves the render
            # state machine consistent.
            self._cancel_requested = True
            self.log.warning("stage %s interrupted by user", stage_name)
            outcome = {"success": False, "cancelled": True, "error": "interrupted"}
        except Exception as exc:  # noqa: BLE001 - stage isolation
            self.log.exception("stage %s crashed", stage_name)
            outcome = {"success": False, "error": f"stage crash: {exc}"}
        outcome = dict(outcome or {})
        ok = bool(outcome.get("success"))
        for warn in outcome.get("warnings") or []:
            ctx["warnings"].append(f"[{stage_name}] {warn}")
        if outcome.get("cancelled"):
            return {
                "stage": stage_name, "status": "cancelled",
                "duration_ms": _ms(started),
            }
        if ok:
            skipped_reason = None
            if isinstance(outcome.get("data"), dict):
                skipped_reason = outcome["data"].get("skipped")
            if skipped_reason:  # module-level self-skip (D.7): honest
                self.event_bus.publish(  # "skipped", not "completed"
                    "pipeline.stage_skipped",
                    {"stage": stage_name, "reason": str(skipped_reason)},
                )
                return {
                    "stage": stage_name, "status": "skipped",
                    "reason": str(skipped_reason),
                    "duration_ms": _ms(started),
                }
            self.event_bus.publish(
                "pipeline.stage_completed",
                {"stage": stage_name, "project_id": ctx.get("project_id")},
            )
            return {
                "stage": stage_name, "status": "completed",
                "data_keys": sorted((outcome.get("data") or {}).keys())
                if isinstance(outcome.get("data"), dict)
                else [],
                "duration_ms": _ms(started),
            }
        error = str(outcome.get("error") or "unknown stage failure")
        if outcome.get("hard_fail"):
            required = True  # stage escalates itself (hard quality gate)
        if required:
            self.event_bus.publish(
                "pipeline.stage_failed", {"stage": stage_name, "error": error}
            )
            return {
                "stage": stage_name, "status": "failed", "error": error,
                "duration_ms": _ms(started),
            }
        ctx["warnings"].append(f"{stage_name} failed (optional): {error}")
        return {
            "stage": stage_name, "status": "warning", "error": error,
            "duration_ms": _ms(started),
        }

    # ------------------------------------------------------------------
    # Stage 0: license
    # ------------------------------------------------------------------
    def _license_instance(self, ctx: Dict[str, Any]) -> Optional[Any]:
        """LicenseManager lives in core/, not the module registry."""
        if ctx.get("enforce_license", True):
            try:
                from core.license_manager import LicenseManager

                return LicenseManager(self.container)
            except Exception as exc:  # noqa: BLE001
                self.log.error("license manager unavailable: %s", exc)
                return None
        return None

    def _stage_license(self, manager: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        checked = manager.check_license()
        data = checked.get("data") or {}
        status = str(data.get("status") or "invalid")
        ctx["license_status"] = status
        if status in _LICENSE_ALLOWED_STATUSES:
            return {"success": True, "data": {"status": status}}
        return {
            "success": False,
            "error": f"license status '{status}' blocks rendering",
            "data": {"status": status, "days_remaining": data.get("days_remaining")},
        }

    # ------------------------------------------------------------------
    # Stage 1: parse + persist (script flow only)
    # ------------------------------------------------------------------
    def _stage_parse(self, parser: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        if ctx.get("project_id"):
            return {"success": True, "data": {"reused_project": True}}
        self._transition(RenderState.LOADING, "parse script")
        parsed = parser.parse_script(ctx["script_path"])
        if not parsed.get("success"):
            return parsed
        data = parsed.get("data") or {}
        ctx["parsed_data"] = data
        project_id = self.db.new_id()
        settings = data.get("project_settings") or {}
        folder = Path(str(ctx["project_folder"]))
        folder.mkdir(parents=True, exist_ok=True)
        title = ctx.get("project_title") or settings.get("title") or "Untitled"
        created = self.db.create_project(
            {
                "id": project_id,
                "title": title,
                "project_folder_path": str(folder),
                "genre": settings.get("genre") or "dark_history",
            }
        )
        if not created:
            return {"success": False, "error": "create_project failed"}
        ctx["project_id"] = project_id
        self._persist_scenes_and_lines(project_id, data, ctx)
        estimate = self._estimate_render_resources(data, ctx)
        ctx["render_estimate"] = estimate
        self.log.info(
            "Pre-render estimate: ~%.0f min runtime, ~%.0f MB output "
            "(disk free: %.0f MB)",
            estimate["estimated_seconds"] / 60.0,
            estimate["estimated_output_mb"],
            estimate["disk_free_mb"],
        )
        self.event_bus.publish("pipeline.render_estimate", estimate)
        if estimate["disk_free_mb"] < estimate["estimated_output_mb"] * 1.5 + 200:
            return {
                "success": False,
                "error": (
                    f"Not enough free disk space: ~{estimate['estimated_output_mb']:.0f} MB "
                    f"needed (plus working room), only {estimate['disk_free_mb']:.0f} MB free. "
                    "Free up space and try again."
                ),
            }
        return {
            "success": True,
            "data": {"project_id": project_id,
                     "scenes": len(data.get("scenes") or [])},
        }

    def _estimate_render_resources(
        self, data: Dict[str, Any], ctx: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Rough pre-render time/size estimate from script content alone.

        FEATURE (v3.2.4): for a long (e.g. 5-hour) project, knowing
        roughly how long a render will take and how much disk space it
        needs BEFORE committing to it matters a lot more than for a quick
        test clip. This is a best-effort estimate from calibrated
        constants (measured against this app's own real render logs), not
        a guarantee — actual hardware varies. Always framed as
        "estimated" to the user, never presented as exact.
        """
        scenes = data.get("scenes") or []
        total_words = 0
        explicit_duration_total = 0.0
        for scene in scenes:
            for line in scene.get("dialogue") or scene.get("lines") or []:
                total_words += len(str(line.get("text") or "").split())
            # Respect an explicit //DURATION: override (e.g. a deliberate
            # long hold on one image) — the render uses whichever is
            # longer: real narration length or this explicit minimum.
            raw_dur = scene.get("duration")
            if raw_dur not in (None, "auto", ""):
                try:
                    explicit_duration_total += float(raw_dur)
                except (TypeError, ValueError):
                    pass
        if total_words == 0:
            # Fallback: whole scenes' raw text if no per-line structure
            for scene in scenes:
                total_words += len(str(scene).split())

        # ~0.4s of narration per word (matches the word-count TTS fallback
        # used elsewhere in this codebase), plus one inter-line pause
        # (0.25s) per scene as a rough approximation of line count.
        est_narration_s = total_words * 0.4 + len(scenes) * 0.25
        # NOTE: explicit //DURATION: overrides only set a MINIMUM on-screen
        # time per scene in this app's current timing model — actual scene
        # display length still comes from real narration length (see the
        # 3.1.7 sync fix), so a "hold this image for 2 minutes" script
        # needs roughly 2 minutes of spoken narration text, not just a
        # duration value, to actually hold that long in the final video.
        est_narration_s = max(est_narration_s, explicit_duration_total)
        est_video_s = est_narration_s + 25.0  # + typical intro/outro

        # Calibrated from real render logs (3.2.1/3.2.2): roughly 1.3s of
        # wall-clock render time per second of *output* video for scene
        # rendering with the pre-bake+parallel optimizations, plus TTS at
        # roughly 2s/line (parallelized) and a flat overhead for join/mux/
        # subtitle burn that scales with total output length.
        est_tts_s = len(scenes) * 2.0
        est_scene_render_s = est_video_s * 1.3
        est_join_burn_s = est_video_s * 0.3 + 20.0
        estimated_seconds = est_tts_s + est_scene_render_s + est_join_burn_s

        # Size: quality-based encoding (v3.2.3) averages roughly 1.5-2.5
        # Mbps effective for typical documentary-style 1080p content —
        # use 2 Mbps as a middle estimate, plus 128kbps audio.
        estimated_output_mb = est_video_s * (2000 + 128) / 8 / 1024

        free_bytes = 0
        try:
            free_bytes = shutil.disk_usage(
                str(ctx.get("project_folder") or ".")
            ).free
        except OSError:
            free_bytes = 0

        return {
            "estimated_seconds": round(estimated_seconds, 1),
            "estimated_video_seconds": round(est_video_s, 1),
            "estimated_output_mb": round(estimated_output_mb, 1),
            "disk_free_mb": round(free_bytes / 1024 / 1024, 1),
            "note": "Estimate only — actual time/size vary by hardware and content.",
        }


    # Kept in sync with modules/image_processor.py's SUPPORTED_FORMATS —
    # duplicated rather than imported to avoid a core->modules import at
    # this layer; both lists are checked by the same test that verifies
    # this feature (see test_all_features.py / the pronunciation-style
    # sync tests added earlier this session for the pattern).
    _IMAGE_EXTENSIONS = (
        ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif",
    )

    def _resolve_image_path(self, images_root: str, image_name: str) -> str:
        """Find a scene's image file, tolerant of format mismatches.

        FEATURE (v3.2.18): a script's //IMAGE: line names a file with a
        SPECIFIC extension (e.g. "scene1.jpg"), but the actual file on
        disk might be a different format entirely — e.g. the user has
        scene1.png instead. Real user report: they had all their images
        ready, just as .png instead of the .jpg the script named, and
        the render failed as "image not found" even though the image
        genuinely existed under a different extension.

        Tries the EXACT filename first (fast path, the common case —
        no behavior change when the extension already matches). If that
        doesn't exist, tries the same basename with every supported
        image format (jpg/jpeg/png/webp/bmp/tiff/tif), case-insensitive
        on the extension, and uses the first one found.
        """
        root = Path(str(images_root))
        exact = root / image_name
        if exact.is_file():
            return str(exact)
        stem = Path(image_name).stem
        if not stem:
            return ""
        for ext in self._IMAGE_EXTENSIONS:
            for candidate_ext in (ext, ext.upper()):
                candidate = root / f"{stem}{candidate_ext}"
                if candidate.is_file():
                    return str(candidate)
        return ""

    def _persist_scenes_and_lines(
        self, project_id: str, data: Dict[str, Any], ctx: Dict[str, Any]
    ) -> None:
        """Write a parsed script's scenes and dialogue lines to the DB.

        PHASE 9 (prevent partial project corruption): a failure part-way
        through — a disk error, an unexpected row shape — used to leave
        the project with SOME of its scenes and dialogue committed,
        which every later stage then treated as the complete script
        (rendering a silently truncated video). The whole persist now
        runs inside one transaction when the backend supports it, so the
        project either has the entire script or none of it and the parse
        stage fails cleanly. Backends without ``transaction`` (DI test
        doubles) keep the previous statement-at-a-time behavior.
        """
        transaction = getattr(getattr(self.db, "db", None), "transaction", None)
        if not callable(transaction):
            self._persist_scenes_and_lines_body(project_id, data, ctx)
            return
        with transaction():
            self._persist_scenes_and_lines_body(project_id, data, ctx)

    def _persist_scenes_and_lines_body(
        self, project_id: str, data: Dict[str, Any], ctx: Dict[str, Any]
    ) -> None:
        images_root = ctx.get("images_folder")
        with self.db.db.transaction():
            for index, scene in enumerate(data.get("scenes") or [], start=1):
                scene_id = self.db.new_id()
                image_name = str(scene.get("image") or "")
                image_path = ""
                if image_name and images_root:
                    resolved = self._resolve_image_path(images_root, image_name)
                    if resolved:
                        image_path = resolved
                    else:
                        # 3.1.0: was silently stored as NULL -> `-i .` at
                        # export. Warn loud and early instead (RULE 7).
                        ctx["warnings"].append(
                            f"Scene {index}: image '{image_name}' not "
                            "found in the images folder (checked all "
                            "supported formats: jpg/jpeg/png/webp/bmp/"
                            "tiff) — export stops until it exists"
                        )
                elif not image_name:
                    ctx["warnings"].append(
                        f"Scene {index}: no image mapped in the script"
                    )
                saved = self.db.save_scene(
                    {
                        "id": scene_id,
                        "project_id": project_id,
                        "scene_number": int(scene.get("scene_number") or index),
                        "image_filename": image_name,
                        "image_file_path": image_path or None,
                        "transition_in": scene.get("transition_in") or "crossfade",
                        "transition_out": scene.get("transition_out") or "crossfade",
                        "animation_type": scene.get("animation") or "ken_burns",
                    }
                )
                if not saved:
                    ctx["warnings"].append(
                        f"scene {index} could not be persisted"
                    )
                    continue
                for line_no, line in enumerate(scene.get("dialogue") or [], start=1):
                    self.db.db.execute(
                        "INSERT INTO dialogue_lines (id, project_id, scene_id,"
                        " line_number, character_name, emotion, text_content,"
                        " pause_after, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            self.db.new_id(),
                            project_id,
                            scene_id,
                            line_no,
                            str(line.get("character") or "NARRATOR").upper(),
                            str(line.get("emotion") or "neutral"),
                            str(line.get("text") or ""),
                            str(line.get("pause_after") or "short"),
                            utc_now_str(),
                            utc_now_str(),
                        ),
                    )

    # ------------------------------------------------------------------
    # Stages 2-4: profile, voices, keywords
    # ------------------------------------------------------------------
    def _stage_channel_profile(
        self, manager: Any, ctx: Dict[str, Any]
    ) -> Dict[str, Any]:
        ref = ctx.get("channel_profile_id")
        if not ref:
            return {"success": True, "data": {"skipped": "no profile ref"}}
        result = manager.apply_profile_to_project(ctx["project_id"], str(ref))
        if result.get("success"):
            return result
        # A stale/missing profile ref must not kill the render.
        return {"success": False, "error": result.get("error") or "apply failed"}

    def _stage_voice_profiles(
        self, manager: Any, ctx: Dict[str, Any]
    ) -> Dict[str, Any]:
        data = ctx.get("parsed_data")
        if data:
            return manager.create_profiles_from_script(data, ctx["project_id"])
        existing = manager.get_all_profiles(ctx["project_id"])
        if existing.get("success") and existing.get("data", {}).get("profiles"):
            return {"success": True, "data": {"reused_existing": True}}
        return {
            "success": False,
            "error": "no parsed script data and no existing profiles",
        }

    def _stage_keywords(self, analyzer: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        return analyzer.analyze_all_scenes(ctx["project_id"])

    # ------------------------------------------------------------------
    # Stage 5: quality gate (advisory by default)
    # ------------------------------------------------------------------
    def _stage_quality_gate(self, checker: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        self._transition(RenderState.VALIDATING, "pre-render checks")
        result = checker.run_full_check(ctx["project_id"])
        if not result.get("success"):
            return result
        data = result.get("data") or {}
        # run_full_check already applied every registered auto-fix.
        readiness = bool(data.get("is_render_ready", True))
        ctx["warnings"].append(
            "quality: " + ("READY" if readiness else "NOT READY")
            + f" ({data.get('passed', '?')}/{data.get('total_checks', '?')})"
        )
        if ctx.get("quality_gate") and not readiness:
            return {
                "success": False,
                "hard_fail": True,
                "error": "quality gate: unresolved critical/error issues",
                "data": {"is_render_ready": False},
            }
        return {"success": True, "data": {"is_render_ready": readiness}}

    # ------------------------------------------------------------------
    # Stages 6-8: images, tts, sfx
    # ------------------------------------------------------------------
    def _stage_images(self, processor: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        self._transition(RenderState.GENERATING, "image processing")
        try:
            result = processor.process_all_images(
                ctx["project_id"],
                cancel_check=lambda: self._cancel_requested,
            )
        except TypeError:
            result = processor.process_all_images(ctx["project_id"])
        if not result.get("success"):
            return result

        # BUGFIX (v3.2.17): the missing-image check used to live only in
        # the export stage (v3.2.1) — which runs AFTER tts/audio_mix/
        # subtitles/timeline. For a long script, that meant a missing
        # image file wasn't discovered until AFTER several minutes (or,
        # for a large script, close to ten minutes) of TTS generation
        # had already completed — confirmed directly from a real user
        # log showing 8 minutes of narration generated before a missing-
        # image failure surfaced. Checking here instead — right after
        # images process, before tts starts — catches the exact same
        # problem in seconds instead of minutes. The export-stage check
        # from v3.2.1 is left in place too as a final safety net (e.g.
        # if a file gets deleted mid-render), just no longer the FIRST
        # line of defense.
        scenes = self.db.db.fetch_all(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY scene_number",
            (ctx["project_id"],),
        )
        missing = []
        for scene in scenes:
            sd = self._scene_for_render(
                scene, {}, str(ctx.get("images_folder") or "")
            )
            img = str(sd.get("image_path") or sd.get("image") or "")
            if not img or not Path(img).is_file():
                missing.append(f"scene {sd.get('scene_number')}: {img or '(empty)'}")
        if missing:
            return {
                "success": False,
                "error": (
                    "missing scene image(s) — caught before TTS generation "
                    "to avoid wasting time on narration for a render that "
                    "can't finish:\n" + "\n".join(missing)
                ),
            }
        return result

    def compute_speed_for_target_duration(
        self, natural_duration: float, target_duration: float,
        current_speed: float = 1.0,
    ) -> Dict[str, Any]:
        """Speed multiplier needed to fit narration into a target length.

        FEATURE (v3.2.16): pure math, no I/O — deliberately kept
        separate from the actual TTS regeneration so the calculation
        itself can be tested in isolation. Clamped to a range that
        still sounds like natural speech (0.6x-1.8x); if the true
        requirement falls outside that range, the target genuinely
        can't be reached without audible distortion — this is reported
        via "clamped"/"achievable" rather than silently producing
        garbled speech.
        """
        if target_duration <= 0 or natural_duration <= 0:
            return {
                "speed": current_speed, "requested_speed": current_speed,
                "clamped": False, "achievable": False,
                "projected_duration_seconds": natural_duration,
            }
        # BUGFIX (caught during testing): originally clamped to
        # 0.6x-1.8x, a range invented for this feature — but the app's
        # own Speed slider (Voice Controls) already allows 0.5x-2.0x
        # and treats that as valid. Using a narrower, inconsistent range
        # here meant the app's OWN documented example (a 5-minute
        # script stretched to 10 minutes = exactly 0.5x) would fail as
        # "unachievable" even though 0.5x is a normal, already-supported
        # speed elsewhere in this same app. Matched to the existing
        # standard instead of inventing a new one.
        min_speed, max_speed = 0.5, 2.0
        required = current_speed * (natural_duration / target_duration)
        clamped_speed = max(min_speed, min(max_speed, required))
        was_clamped = abs(clamped_speed - required) > 1e-6
        projected = natural_duration * (current_speed / clamped_speed)
        return {
            "speed": round(clamped_speed, 3),
            "requested_speed": round(required, 3),
            "clamped": was_clamped,
            "achievable": not was_clamped,
            "projected_duration_seconds": round(projected, 1),
        }

    def _stage_tts(self, tts: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        lines = self.db.db.fetch_all(
            "SELECT dl.*, s.scene_number FROM dialogue_lines dl"
            " JOIN scenes s ON s.id = dl.scene_id"
            " WHERE dl.project_id = ?"
            " ORDER BY s.scene_number, dl.line_number",
            (ctx["project_id"],),
        )
        preview_limit = ctx.get("preview_max_scenes")
        if preview_limit:
            # Preview mode (v3.2.4) generates narration only for the
            # scenes actually being previewed — without this, TTS would
            # still process an entire long script even though only a
            # handful of scenes get rendered, defeating the point of a
            # quick preview.
            lines = [
                ln for ln in lines
                if int(ln.get("scene_number") or 0) <= int(preview_limit)
            ]
        if not lines:
            return {"success": False, "error": "no dialogue lines to synthesize"}
        audio_dir = Path(str(ctx["project_folder"])) / "audio"
        # PHASE 9: an uncreatable project folder is reported as a clean
        # stage failure instead of an OSError raised out of the handler.
        if ensure_directory(audio_dir) is None:
            return {
                "success": False,
                "error": f"cannot create narration folder: {audio_dir}",
            }
        profiles = self._modules.get("voice_profile_manager")

        # FEATURE (v3.2.13): the "Pronunciation:" field in Voice Controls
        # previously saved a file path that nothing ever read. Loaded
        # once here (not per-line — cheap, and the dict doesn't change
        # mid-render) and applied to every line's text before TTS.
        pronunciation_path = str(self.config.get("voice_pronunciation", "") or "")
        pronunciation_dict = (
            tts.load_pronunciation_dict(pronunciation_path)
            if pronunciation_path else {}
        )

        # Resolve text + voice profile for every line up front (cheap, DB
        # reads only) — needed before we can generate anything, parallel
        # or not.
        jobs: List[Dict[str, Any]] = []
        for index, row in enumerate(lines):
            text = str(row.get("text_content") or "").strip()
            if not text:
                continue
            if pronunciation_dict:
                text = tts.apply_pronunciation_dict(text, pronunciation_dict)
            character = str(row.get("character_name") or "NARRATOR")
            char_profile = self._character_profile(
                profiles, ctx["project_id"], character
            )
            # FEATURE (v3.2.16): target-duration fitting. A multiplier
            # is applied on top of each character's own configured
            # speed when the pipeline re-invokes this stage a second
            # time to correct for a target duration — see
            # compute_speed_for_target_duration() and _run()'s handling
            # right after the "tts" stage. Defaults to 1.0 (no-op) for
            # every normal render — this line changes nothing unless
            # ctx["speed_multiplier"] was explicitly set.
            multiplier = float(ctx.get("speed_multiplier") or 1.0)
            char_profile = dict(char_profile)
            if multiplier != 1.0:
                char_profile["speed"] = (
                    float(char_profile.get("speed") or 1.0) * multiplier
                )
            emotion = str(
                row.get("emotion") or char_profile.get("default_emotion")
                or "neutral"
            )
            # PHASE 7: carry the authored line emotion into generation.
            # TTSEngineManager still retains default_emotion as a fallback,
            # but no longer loses row-level changes behind that default.
            char_profile["emotion"] = emotion
            jobs.append({
                "index": index, "row": row, "text": text,
                "character": character, "profile": char_profile,
                "out_path": audio_dir / f"line_{index:03d}.wav",
                # PHASE 5 (natural narration / paragraph-based TTS):
                # extra keys used only by group_sentences_into_paragraphs
                # — harmless additions for every other consumer of this
                # dict, which only ever reads the keys above.
                "emotion": emotion,
                "pause_after": str(row.get("pause_after") or "none"),
            })
        if not jobs:
            return {"success": False, "error": "no dialogue lines to synthesize"}

        self._attach_narration_prosody(jobs)
        groups = self._group_tts_jobs(tts, ctx, jobs, audio_dir)

        def _generate_unit(group: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
            return self._generate_tts_unit(tts, group, audio_dir)

        # PERFORMANCE (v3.2.2, extended Phase 5): TTS generation is
        # parallelized across UNITS — each unit is either a single line
        # (sentence mode / paragraph mode disabled / a group that
        # couldn't be safely merged) or a whole paragraph (several
        # consecutive compatible lines synthesized as ONE TTS request,
        # then split back into individual per-line files — see
        # _generate_tts_unit). For a long script this was previously the
        # single biggest serial wait after scene rendering. Ordering-
        # sensitive work (offset_seconds accumulation, word-timestamp
        # insertion — the same math the sync fixes in 3.1.5-3.1.9 depend
        # on) still happens in a SEPARATE sequential pass below, over the
        # flattened per-line results, in original line order — neither
        # parallelism nor paragraph batching ever reorders anything that
        # sync correctness depends on.
        #
        # The first unit is generated alone, sequentially, before the
        # pool starts: this guarantees the TTS engine's one-time lazy
        # model load (e.g. Kokoro) happens in a single thread, not racing
        # across multiple threads on their first call. Once loaded,
        # concurrent inference calls are safe (the underlying onnxruntime
        # session supports concurrent .run() by design).
        first, rest = groups[0], groups[1:]
        outcomes: Dict[int, Dict[str, Any]] = dict(_generate_unit(first))
        if rest:
            max_workers = max(1, min(4, (os.cpu_count() or 2)))
            cancelled = False
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_generate_unit, g): g for g in rest}
                for future in as_completed(futures):
                    if self._cancel_requested:
                        # PHASE 9 (safe cancellation / thread cleanup):
                        # returning from inside the `with` still blocks
                        # in ThreadPoolExecutor.__exit__ until EVERY
                        # queued unit has been synthesized — a cancel on
                        # a long script appeared to hang for minutes.
                        # Un-started units are cancelled first so the
                        # pool drains promptly; work already in flight
                        # is still awaited (killing a thread mid-write
                        # is what leaves corrupt line WAVs behind).
                        cancelled = True
                        for pending in futures:
                            pending.cancel()
                        break
                    outcomes.update(future.result())
            if cancelled:
                return {
                    "success": False, "cancelled": True,
                    "error": "pipeline cancelled during tts",
                }

        line_paths: List[str] = []
        offset_seconds = 0.0
        generated = 0
        failures = 0
        # PHASE 6 (natural pauses & human pacing): plan the gap that
        # follows every successfully-generated line ONCE, from the real
        # (measured) durations, then reuse that same plan for the
        # word-timestamp offsets below, for the narration assembly in
        # _stage_audio_mix, and — recomputed identically from the same
        # DB rows — for timeline_engine's scene durations. A single
        # shared plan is what keeps voice, images and subtitles aligned
        # now that the gaps vary instead of being a flat constant.
        pacing = self._pacing_config()
        accepted: List[Dict[str, Any]] = []
        for job in jobs:  # original order — required for correct offsets
            index, row = job["index"], job["row"]
            gen = outcomes[index]["result"]
            if not gen.get("success"):
                failures += 1
                ctx["warnings"].append(
                    f"tts line {index} ({job['character']}): {gen.get('error')}"
                )
                continue
            gdata = gen.get("data") or {}
            audio_path = _pick_path(gdata, "audio_path") or str(job["out_path"])
            duration = float(gdata.get("duration") or 0.5)
            # FEATURE (v3.2.4): narration QA — flag lines whose generated
            # audio duration is wildly off from what the text length would
            # suggest (a TTS glitch: cut-off speech, stuck/looping output,
            # or a near-empty render). Using the same ~0.35s/word estimate
            # already used elsewhere in this codebase as the fallback
            # duration heuristic, so the threshold is consistent with how
            # the rest of the pipeline reasons about expected speech
            # length. This only WARNS — it never blocks the render, since
            # unusual pacing can be legitimate (e.g. deliberately slow
            # delivery); it's meant to help you spot lines worth a manual
            # listen before publishing, not to auto-reject anything.
            word_count = len(job["text"].split())
            if word_count > 0:
                expected = word_count * 0.35
                if duration < expected * 0.4:
                    ctx["warnings"].append(
                        f"QA: line {index} ({job['character']}) audio is "
                        f"much shorter ({duration:.1f}s) than its "
                        f"{word_count} words would suggest (~{expected:.1f}s "
                        "expected) — possibly cut off, worth a listen"
                    )
                elif duration > expected * 3.0 and duration > 3.0:
                    ctx["warnings"].append(
                        f"QA: line {index} ({job['character']}) audio is "
                        f"much longer ({duration:.1f}s) than its "
                        f"{word_count} words would suggest (~{expected:.1f}s "
                        "expected) — possibly stuck/looping, worth a listen"
                    )
            self.db.db.execute(
                "UPDATE dialogue_lines SET audio_generated = 1,"
                " audio_file_path = ?, audio_duration = ?,"
                " word_timestamps_json = ?, status = 'completed'"
                " WHERE id = ?",
                (
                    str(audio_path),
                    duration,
                    json.dumps(gdata.get("word_timestamps") or []),
                    row["id"],
                ),
            )
            accepted.append(
                {
                    "job": job,
                    "row": row,
                    "duration": duration,
                    "word_timestamps": gdata.get("word_timestamps") or [],
                    # PHASE 6: paragraph-batched lines carry the engine's
                    # own prosodic sentence pause in their clip tail
                    # (PHASE 5) — the planner uses this to avoid stacking
                    # a second, full-length gap on top of it.
                    "paragraph_internal": bool(gdata.get("paragraph_batched")),
                }
            )
            line_paths.append(str(audio_path))
            generated += 1
        if generated == 0:
            return {
                "success": False,
                "error": f"tts produced no audio ({failures} line failures)",
            }

        # PHASE 6: now that every accepted line has a REAL measured
        # duration, plan all inter-line gaps in one pass. The last
        # entry (the trailing gap after the final line) is dropped —
        # narration never ends with silence.
        line_pauses = self._plan_line_pauses(accepted, pacing)
        with self.db.db.transaction():
            for position, entry in enumerate(accepted):
                self._insert_word_timestamps(
                    ctx["project_id"],
                    entry["row"]["id"],
                    entry["word_timestamps"],
                    offset_seconds,
                )
                offset_seconds += entry["duration"] + line_pauses[position]
        # offset_seconds now includes the trailing gap after the last
        # line, which is never rendered — subtract it back off exactly
        # as the flat-constant version did.
        trailing = line_pauses[-1] if line_pauses else 0.0
        ctx["line_audio_paths"] = line_paths
        # PHASE 6: the gaps _stage_audio_mix must insert between these
        # exact files (one per join, so len(line_paths) - 1 entries).
        ctx["line_pause_seconds"] = line_pauses[:-1] if line_pauses else []
        # PHASE 6: the SAME plan keyed by dialogue_line id, handed to
        # timeline_engine so scene durations are computed from the gaps
        # actually rendered rather than re-derived. Re-deriving would be
        # close but not exact — only this stage knows which lines were
        # paragraph-batched and which failed to generate — and "close"
        # compounds into visible image/subtitle drift across a long
        # script (the v3.1.7 bug class this pipeline already fixed once).
        ctx["line_pause_by_id"] = {
            str(entry["row"]["id"]): line_pauses[position]
            for position, entry in enumerate(accepted)
        }
        ctx["narration_duration"] = max(0.0, offset_seconds - trailing)
        return {
            "success": True,
            "data": {"lines_generated": generated, "lines_failed": failures},
        }

    def _attach_narration_prosody(
        self, jobs: List[Dict[str, Any]]
    ) -> None:
        """Attach PHASE 7 context-aware plans without changing job order.

        Planning all lines together lets rate and first-phrase contours
        ease across emotion changes and lets emphasis account for nearby
        repeated vocabulary.  The plan is text-only and O(words); no model,
        audio, or extra synthesis request is involved.  Any failure disables
        the enhancement for this render and leaves the Phase 1-6 path intact.
        """
        if not jobs:
            return
        try:
            config = resolve_prosody_config(self.config)
            plans = plan_narration_prosody(jobs, config)
            if len(plans) != len(jobs):
                raise ValueError("prosody planner returned an invalid plan count")
        except Exception as exc:  # noqa: BLE001 - prosody is never fatal
            self.log.warning(
                "Narration prosody planning failed (%s) — using existing "
                "engine delivery", exc,
            )
            plans = [
                {"enabled": False, "phrases": [], "emphasis": []}
                for _ in jobs
            ]
        for job, plan in zip(jobs, plans):
            job["prosody_plan"] = plan
            profile = dict(job["profile"])
            profile["prosody_plan"] = plan
            job["profile"] = profile

    def _pacing_config(self) -> Dict[str, Any]:
        """Resolved PHASE 6 narration pacing config for this render.

        Read from ``config/app_settings.json`` via the injected config
        service (see ``core.narration_pacing.SETTING_KEYS``). Never
        raises: any problem falls back to the module defaults, and
        ``natural_pauses_enabled = false`` restores the flat Phase 1-5
        gap everywhere.
        """
        try:
            return resolve_pacing_config(self.config)
        except Exception as exc:  # noqa: BLE001 - pacing is never fatal
            self.log.warning(
                "Narration pacing config unavailable (%s) — using defaults", exc
            )
            return resolve_pacing_config(None)

    @staticmethod
    def _accepts_kwarg(func: Any, name: str) -> bool:
        """Whether ``func`` can be called with keyword argument ``name``.

        PHASE 6: lets this orchestrator pass a new optional argument to a
        module without breaking DI test doubles or an older/alternate
        implementation that predates it (RULE 1's seam stays additive).
        Returns True for ``**kwargs``-style callables and False whenever
        the signature can't be inspected — the conservative choice, since
        omitting the argument always degrades to documented behavior.
        """
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):  # pragma: no cover - builtins/C
            return False
        for parameter in signature.parameters.values():
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                return True
            if parameter.name == name and parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                return True
        return False

    def _plan_line_pauses(
        self, accepted: List[Dict[str, Any]], pacing: Dict[str, Any]
    ) -> List[float]:
        """Plan the gap after every generated narration line.

        PHASE 6 (natural pauses & human pacing): delegates to the shared
        planner so this orchestrator, audio_processor and timeline_engine
        all derive the identical timing from the same inputs. Falls back
        to the flat legacy constant on any failure, so a planning bug can
        never cost a render more than the naturalness it was adding.
        """
        if not accepted:
            return []
        try:
            plan = plan_narration_pauses(
                [
                    {
                        "text": entry["job"]["text"],
                        "character": entry["job"]["character"],
                        "emotion": entry["job"].get("emotion") or "neutral",
                        "pause_after": entry["job"].get("pause_after") or "",
                        "scene_id": entry["row"].get("scene_id"),
                        "duration": entry["duration"],
                        "paragraph_internal": entry["paragraph_internal"],
                    }
                    for entry in accepted
                ],
                pacing,
            )
        except Exception as exc:  # noqa: BLE001 - never break a render
            self.log.warning(
                "Narration pause planning failed (%s) — using the flat "
                "%.2fs gap for this render", exc, _PAUSE_BETWEEN_LINES,
            )
            return [_PAUSE_BETWEEN_LINES] * len(accepted)
        if len(plan) != len(accepted):  # pragma: no cover - defensive
            return [_PAUSE_BETWEEN_LINES] * len(accepted)
        return plan

    def _group_tts_jobs(
        self,
        tts: Any,
        ctx: Dict[str, Any],
        jobs: List[Dict[str, Any]],
        audio_dir: Path,
    ) -> List[List[Dict[str, Any]]]:
        """Group TTS jobs into paragraph-sized batches when safe/possible.

        PHASE 5 (natural narration / paragraph-based TTS): returns a list
        of job-groups — each group is either one job (sentence mode) or
        several consecutive, compatible jobs to be synthesized together
        as one paragraph (see TTSEngineManager.group_sentences_into_
        paragraphs for the exact compatibility rules).

        Paragraph batching is used ONLY when all of the following hold,
        so it degrades to the exact original one-request-per-line
        behavior automatically and safely otherwise:

          * ``app_settings.paragraph_narration_enabled`` is not
            explicitly disabled (default: enabled).
          * The active ``tts`` module actually implements
            ``group_sentences_into_paragraphs``/``generate_paragraph_audio``
            (real TTSEngineManager does; a minimal DI test double or a
            future alternate implementation that doesn't is simply
            treated as sentence-mode-only — this is what keeps every
            existing DI-based test passing unmodified).
          * The active ``audio_processor`` module implements
            ``split_paragraph_audio`` (needed to recover per-line files
            from a paragraph clip for full backward compatibility).
        """
        paragraph_enabled = bool(
            self.config.get("paragraph_narration_enabled", True)
        )
        audio = self._modules.get("audio_processor")
        supports_paragraphs = (
            paragraph_enabled
            and hasattr(tts, "group_sentences_into_paragraphs")
            and hasattr(tts, "generate_paragraph_audio")
            and audio is not None
            and hasattr(audio, "split_paragraph_audio")
        )
        if not supports_paragraphs:
            return [[job] for job in jobs]
        try:
            groups = tts.group_sentences_into_paragraphs(jobs)
        except Exception as exc:  # noqa: BLE001 - never let batching crash TTS
            self.log.warning(
                "Paragraph grouping failed (%s) — falling back to sentence "
                "mode for this render", exc,
            )
            return [[job] for job in jobs]
        return groups if groups else [[job] for job in jobs]

    def _generate_tts_unit(
        self, tts: Any, group: List[Dict[str, Any]], audio_dir: Path
    ) -> Dict[int, Dict[str, Any]]:
        """Generate one unit (single line or paragraph) of TTS jobs.

        Returns a mapping of ``job["index"] -> {**job, "result": gen}``
        for every job in ``group`` — the exact shape _stage_tts's
        post-processing loop already expects, regardless of whether the
        unit was one line or a whole paragraph. Never raises: any
        internal failure falls back to per-line sentence-mode generation
        for this group instead of losing the group's narration entirely.
        """
        if len(group) == 1:
            job = group[0]
            gen = tts.generate_audio(job["text"], job["profile"], job["out_path"])
            return {job["index"]: {**job, "result": gen}}
        return self._generate_paragraph_unit(tts, group, audio_dir)

    def _generate_paragraph_unit(
        self, tts: Any, group: List[Dict[str, Any]], audio_dir: Path
    ) -> Dict[int, Dict[str, Any]]:
        """Synthesize a paragraph group as one TTS request, then split.

        PHASE 5: on ANY failure (paragraph synthesis error, split error,
        exception) this falls all the way back to generating every line
        in the group individually via generate_audio — "sentence mode"
        — so a paragraph-batching problem never costs the render more
        than the efficiency gain it was trying to provide.
        """
        audio = self._modules.get("audio_processor")
        first = group[0]
        paragraph_path = audio_dir / f"paragraph_{first['index']:03d}.wav"
        try:
            gen = tts.generate_paragraph_audio(
                group, first["profile"], paragraph_path
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "Paragraph TTS generation raised (%s) — falling back to "
                "sentence mode for this paragraph", exc,
            )
            return self._generate_sentence_fallback(tts, group)
        if not gen.get("success"):
            self.log.warning(
                "Paragraph TTS generation failed (%s) — falling back to "
                "sentence mode for this paragraph", gen.get("error"),
            )
            return self._generate_sentence_fallback(tts, group)

        gdata = gen.get("data") or {}
        line_breakdown = gdata.get("line_breakdown") or []
        if len(line_breakdown) != len(group):
            self.log.warning(
                "Paragraph line_breakdown length mismatch (%d vs %d lines) "
                "— falling back to sentence mode for this paragraph",
                len(line_breakdown), len(group),
            )
            return self._generate_sentence_fallback(tts, group)

        try:
            split = audio.split_paragraph_audio(
                gdata["audio_path"],
                line_breakdown,
                audio_dir,
                output_paths=[job["out_path"] for job in group],
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "Paragraph audio split raised (%s) — falling back to "
                "sentence mode for this paragraph", exc,
            )
            return self._generate_sentence_fallback(tts, group)
        if not split.get("success"):
            self.log.warning(
                "Paragraph audio split failed (%s) — falling back to "
                "sentence mode for this paragraph", split.get("error"),
            )
            return self._generate_sentence_fallback(tts, group)

        # Best-effort cleanup of the intermediate combined paragraph
        # file — the split, per-line files are now the source of truth
        # (matches every other stage's file layout); never fatal if it
        # can't be removed (e.g. Windows file lock).
        try:
            Path(gdata["audio_path"]).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

        split_data = split.get("data") or {}
        line_paths = split_data.get("line_paths") or []
        durations = split_data.get("durations") or []
        clip_start_offsets = split_data.get("clip_start_offsets") or [
            float(e.get("start", 0.0)) for e in line_breakdown
        ]
        outcomes: Dict[int, Dict[str, Any]] = {}
        for job, entry, line_path, duration, clip_start in zip(
            group, line_breakdown, line_paths, durations, clip_start_offsets
        ):
            # PHASE 5 (timestamp preservation): generate_paragraph_audio's
            # line_breakdown word_timestamps are PARAGRAPH-relative
            # (seconds from the start of the whole combined clip), but
            # every other engine path (generate_audio) returns LINE-
            # relative timestamps starting near 0 — and _stage_tts's
            # caller (_insert_word_timestamps) adds its own cumulative
            # offset_seconds on top, assuming line-relative input. Left
            # un-adjusted, every line after the first in a paragraph
            # would have its timestamps counted twice (once from the
            # paragraph position, once from the accumulated line
            # offset). Rebase using the SPLIT's actual clip_start_offset
            # (not the raw word-boundary "start") since
            # split_paragraph_audio's small safety margin can shift the
            # real per-file sample-0 position earlier than that — this
            # keeps every timestamp exact, not approximate.
            rebased_words = [
                {
                    "word": w.get("word"),
                    "start": round(max(0.0, float(w.get("start", 0.0)) - clip_start), 3),
                    "end": round(max(0.0, float(w.get("end", 0.0)) - clip_start), 3),
                }
                for w in (entry.get("word_timestamps") or [])
            ]
            line_gen = {
                "success": True,
                "data": {
                    "audio_path": line_path,
                    "duration": duration,
                    "word_timestamps": rebased_words,
                    "engine": gdata.get("engine"),
                    "emotion": gdata.get("emotion"),
                    "params": gdata.get("params"),
                    "paragraph_batched": True,
                    "prosody_plan": job.get("prosody_plan"),
                    "prosody_applied": gdata.get("prosody_applied", False),
                },
            }
            outcomes[job["index"]] = {**job, "result": line_gen}
        return outcomes

    def _generate_sentence_fallback(
        self, tts: Any, group: List[Dict[str, Any]]
    ) -> Dict[int, Dict[str, Any]]:
        """Generate every job in a group individually (sentence mode)."""
        outcomes: Dict[int, Dict[str, Any]] = {}
        for job in group:
            gen = tts.generate_audio(job["text"], job["profile"], job["out_path"])
            outcomes[job["index"]] = {**job, "result": gen}
        return outcomes

    def _character_profile(
        self, profiles: Optional[Any], project_id: str, character: str
    ) -> Dict[str, Any]:
        """Voice profile for a character (alias-resolved, safe defaults)."""
        row: Optional[Dict[str, Any]] = None
        if profiles is not None:
            canonical = character
            try:
                resolved = profiles.resolve_character_alias(project_id, character)
                if resolved:
                    canonical = resolved
                loaded = profiles.load_profile(project_id, canonical)
                if loaded.get("success"):
                    row = (loaded.get("data") or {}).get("profile")
            except Exception:  # noqa: BLE001 - defaults are fine
                row = None
        row = row or {}
        return {
            "engine": row.get("engine") or "piper",
            "voice_model": row.get("voice_model") or "default",
            "speed": float(row.get("speed") or 1.0),
            "pitch": float(row.get("pitch") or 0.0),
            "volume": float(row.get("volume") or 1.0),
            "default_emotion": row.get("default_emotion") or "neutral",
            "reverb_preset": row.get("reverb_preset") or "none",
            "eq_preset": row.get("eq_preset") or "flat",
            "breathing_enabled": bool(row.get("breathing_enabled")),
            # PHASE 2 (voice effects chain rebuild): these voice_profiles
            # columns already existed in the schema (compression_enabled,
            # noise_gate_enabled) but were silently dropped here before
            # reaching TTSEngineManager.apply_voice_effects, so a user's
            # saved per-character bypass choice never actually took
            # effect. Passed through now — defaults match the existing
            # schema column defaults, so any profile that never set these
            # explicitly keeps its exact previous behavior.
            "compression_enabled": bool(row.get("compression_enabled", True)),
            "noise_gate_enabled": bool(row.get("noise_gate_enabled", True)),
            "special_effect": row.get("special_effect") or "none",
        }

    def _insert_word_timestamps(
        self,
        project_id: str,
        dialogue_line_id: str,
        words: List[Dict[str, Any]],
        offset_seconds: float,
    ) -> None:
        """Persist TTS word timings as narration-absolute milliseconds.

        generate_audio's word dicts are line-relative seconds
        ({word, start, end}); subtitles need absolute ms across the
        joined narration track, so the orchestrator accumulates line
        durations + the narration pause constant as it goes (this is
        wiring glue, not module logic - it lives here, not in a module).
        PHASE 10: batched database insert via execute_many / transaction.
        """
        tuples = []
        for word_index, word in enumerate(words):
            text = str(word.get("word") or word.get("word_text") or "")
            if not text:
                continue
            start_s = float(word.get("start") or word.get("start_time_ms", 0.0))
            end_s = float(word.get("end") or word.get("end_time_ms", 0.0))
            if word.get("start_time_ms") is not None:
                start_ms = int(float(word["start_time_ms"]) + offset_seconds * 1000)
            else:
                start_ms = int((start_s + offset_seconds) * 1000)
            if word.get("end_time_ms") is not None:
                end_ms = int(float(word["end_time_ms"]) + offset_seconds * 1000)
            else:
                end_ms = int((end_s + offset_seconds) * 1000)
            tuples.append(
                (
                    self.db.new_id(),
                    project_id,
                    dialogue_line_id,
                    word_index,
                    text,
                    max(0, start_ms),
                    max(0, end_ms),
                )
            )
        if tuples:
            sql = (
                "INSERT INTO word_timestamps (id, project_id,"
                " dialogue_line_id, word_index, word_text, start_time_ms,"
                " end_time_ms) VALUES (?, ?, ?, ?, ?, ?, ?)"
            )
            if hasattr(self.db.db, "execute_many"):
                self.db.db.execute_many(sql, tuples)
            else:
                with self.db.db.transaction():
                    for params in tuples:
                        self.db.db.execute(sql, params)

    def _stage_sfx(self, sfx: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        library = sfx.load_sfx_library()
        if not library.get("success"):
            return library
        placed = sfx.auto_place_sfx(ctx["project_id"])
        if not placed.get("success"):
            return placed
        prepared = sfx.prepare_sfx_for_mixing(ctx["project_id"])
        if prepared.get("success"):
            ctx["sfx_list"] = (prepared.get("data") or {}).get("sfx_list") or []
        return {"success": True, "data": {"prepared": len(ctx.get("sfx_list") or [])}}

    # ------------------------------------------------------------------
    # Stage 9: final mix; Stage 10: SRT
    # ------------------------------------------------------------------
    def _stage_audio_mix(self, audio: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        music = None
        project = self.db.db.fetch_one(
            "SELECT music_file_path FROM projects WHERE id = ?",
            (ctx["project_id"],),
        )
        if project and project.get("music_file_path"):
            music = str(project["music_file_path"])
        out_path = Path(str(ctx["project_folder"])) / "audio" / "final_mix.wav"
        settings = {
            "line_paths": ctx.get("line_audio_paths") or [],
        }
        # PHASE 6 (natural pauses & human pacing): hand the mixer the
        # exact per-join gaps _stage_tts already used for the word-
        # timestamp offsets, so the assembled WAV and every subtitle/
        # scene time are computed from ONE plan rather than two
        # independently-derived ones. Absent (e.g. a resumed render
        # whose ctx predates this key), build_narration_track falls back
        # to its flat pause_seconds default exactly as before.
        pause_plan = ctx.get("line_pause_seconds")
        if pause_plan:
            settings["pause_plan"] = list(pause_plan)
        if music:
            settings["music_path"] = music
        if ctx.get("sfx_list"):
            settings["sfx_list"] = ctx["sfx_list"]
        mixed = audio.generate_final_mix(ctx["project_id"], out_path, settings)
        if not mixed.get("success"):
            return mixed
        ctx["final_mix"] = _pick_path(mixed.get("data") or {}, "audio_path") or str(
            out_path
        )
        return {"success": True, "data": {"final_mix": ctx["final_mix"]}}

    def _stage_subtitles(self, engine: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        self._transition(RenderState.PROCESSING, "subtitles")
        project = self.db.db.fetch_one(
            "SELECT has_subtitles FROM projects WHERE id = ?",
            (ctx["project_id"],),
        )
        if project and not int(project.get("has_subtitles") or 0):
            return {"success": True, "data": {"skipped": "has_subtitles=0"}}
        # BUGFIX (v3.1.5): intro_outro now runs before this stage (see
        # _STAGE_MODULES order), so we know the real intro duration and can
        # shift subtitle timing to match when narration actually starts
        # playing in the final video, instead of assuming it starts at 0.
        intro_duration = float(
            (ctx.get("intro_outro_segments") or {}).get("intro", {}).get(
                "duration"
            )
            or 0.0
        )
        result = engine.generate_srt_from_word_timestamps(
            ctx["project_id"], {"offset_ms": int(intro_duration * 1000)}
        )
        if result.get("success"):
            data = result.get("data") or {}
            ctx["srt_path"] = _pick_path(
                data, "srt_path", "file_path", "output_path"
            )
        return result

    # ------------------------------------------------------------------
    # Stage 11: intro/outro; Stage 12: timeline
    # ------------------------------------------------------------------
    def _stage_intro_outro(self, engine: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        # FEATURE (v3.2.3): intro/outro used a fixed configured duration
        # (e.g. 20s outro) regardless of how long the actual narration is
        # — for a short script this let a static branding card dominate
        # most of the video. Now each is capped to at most half the
        # narration length, floor 3s so it never becomes awkwardly short,
        # and — critically — never exceeding the configured default (5s
        # intro / 20s outro): the cap only ever SHRINKS the card for short
        # scripts, it must never grow it for long ones. A 5-hour video's
        # half-narration figure (9000s) would otherwise balloon the outro
        # to 2.5 hours; clamping to the default keeps long videos exactly
        # as before.
        narration_duration = float(ctx.get("narration_duration") or 0.0)
        intro_overrides = None
        outro_overrides = None
        if narration_duration > 0:
            half = narration_duration * 0.5
            intro_overrides = {"duration": min(5.0, max(3.0, half))}
            outro_overrides = {"duration": min(20.0, max(3.0, half))}
        intro = engine.generate_intro(ctx["project_id"], overrides=intro_overrides)
        outro = engine.generate_outro(ctx["project_id"], overrides=outro_overrides)
        made: Dict[str, Any] = {}
        for key, result in (("intro", intro), ("outro", outro)):
            data = (result or {}).get("data") or {}
            if result.get("success") and not data.get("skipped"):
                path = _pick_path(data, "segment_path", "output_path", "video_path")
                if path:
                    made[key] = {
                        "path": path,
                        "duration": float(data.get("duration") or 0.0),
                    }
        ctx["intro_outro_segments"] = made
        return {"success": True, "data": {"segments": sorted(made)}}

    def _stage_timeline(self, engine: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        io_config = None
        intro_engine = self._modules.get("intro_outro_engine")
        if intro_engine is not None and getattr(intro_engine, "enabled", True):
            try:
                resolved = intro_engine.get_intro_outro_settings(ctx["project_id"])
                if resolved.get("success"):
                    io_config = resolved.get("data") or {}
            except Exception:  # noqa: BLE001 - timeline defaults suffice
                io_config = None
        intro_cfg = (io_config or {}).get("intro") or {}
        outro_cfg = (io_config or {}).get("outro") or {}
        build_kwargs: Dict[str, Any] = {
            "narration_path": ctx.get("final_mix"),
            "intro_config": {
                "enabled": bool(intro_cfg.get("enabled", False)),
                "duration": float(intro_cfg.get("duration") or 5.0),
                "type": "generated",
            },
            "outro_config": {
                "enabled": bool(outro_cfg.get("enabled", False)),
                "duration": float(outro_cfg.get("duration") or 20.0),
                "type": "generated",
            },
            "save": True,
        }
        # PHASE 6 (natural pauses & human pacing): hand the timeline the
        # EXACT per-line gaps this render used, so scene boundaries land
        # on the real audio transitions now that gaps vary per line.
        # Passed only when the receiving implementation accepts it — a
        # DI test double or an alternate TimelineEngine with the older
        # signature keeps working untouched (it then falls back to its
        # own equivalent planning, which stays correct for every line
        # this stage didn't have to special-case).
        pause_by_id = ctx.get("line_pause_by_id")
        if pause_by_id and self._accepts_kwarg(engine.build_timeline, "line_pauses"):
            build_kwargs["line_pauses"] = pause_by_id
        built = engine.build_timeline(ctx["project_id"], **build_kwargs)
        if not built.get("success"):
            return built
        timeline = (built.get("data") or {}).get("timeline") or {}
        if ctx.get("final_mix"):
            timeline["audio_path"] = ctx["final_mix"]
        # BUGFIX (v3.1.6): narration audio was always muxed starting at
        # t=0 of the final video, even though the video itself starts with
        # a silent intro card. Subtitles were fixed (v3.1.5) to wait for
        # the intro, but the actual VOICE kept playing from t=0 regardless
        # — so narration played early/during the intro, finishing well
        # before the (now correctly-timed) captions and images caught up.
        # Delay the audio by the same real intro duration used for the
        # subtitle offset, so voice, captions, and video all start
        # together at the same point.
        intro_duration = float(
            (ctx.get("intro_outro_segments") or {}).get("intro", {}).get(
                "duration"
            )
            or 0.0
        )
        if intro_duration > 0:
            timeline["audio_delay_ms"] = int(intro_duration * 1000)
        ctx["timeline"] = timeline
        return {
            "success": True,
            "data": {"total_duration": timeline.get("total_duration")},
        }

    # ------------------------------------------------------------------
    # Stages 13-14: export render + join
    # ------------------------------------------------------------------
    def _stage_export(self, engine: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        self._transition(RenderState.RENDERING, "scene renders")
        timeline = ctx.get("timeline") or {}
        preset = ctx.get("export_preset")
        animation_engine = self._modules.get("animation_engine")
        grade_engine = self._modules.get("color_grade_engine")
        project = self.db.db.fetch_one(
            "SELECT color_grade_preset FROM projects WHERE id = ?",
            (ctx["project_id"],),
        )
        grade_name = str((project or {}).get("color_grade_preset") or "")
        grade_filter = ""
        grade_extras: Dict[str, Any] = {}
        if grade_engine is not None and grade_name:
            built = grade_engine.build_grade_filter(grade_name)
            if built.get("success"):
                gdata = built.get("data") or {}
                grade_filter = str(gdata.get("filtergraph") or "")
                grade_extras = gdata.get("extras") or {}
            else:
                ctx["warnings"].append(
                    f"grade '{grade_name}' unavailable: {built.get('error')}"
                )

        scenes = self.db.db.fetch_all(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY scene_number",
            (ctx["project_id"],),
        )
        if not scenes:
            return {"success": False, "error": "no scenes to render"}
        preview_limit = ctx.get("preview_max_scenes")
        if preview_limit:
            scenes = scenes[: max(1, int(preview_limit))]
            self.log.info(
                "Preview mode: rendering only the first %d scene(s)",
                len(scenes),
            )
        segments_dir = Path(str(ctx["project_folder"])) / "segments"
        # PHASE 9: same clean-failure treatment as the narration folder.
        if ensure_directory(segments_dir) is None:
            return {
                "success": False,
                "error": f"cannot create segments folder: {segments_dir}",
            }
        segments: List[Dict[str, Any]] = []
        io_segments = ctx.get("intro_outro_segments") or {}
        if io_segments.get("intro"):
            segments.append(
                {"path": io_segments["intro"]["path"],
                 "duration": io_segments["intro"]["duration"] or 5.0}
            )
        # VALIDATION (v3.2.1): check every scene's image exists BEFORE
        # spending any render time — a missing file used to only surface
        # after already rendering earlier scenes.
        missing_images = []
        # PERFORMANCE (PHASE 8): resolving a scene costs a DB lookup plus
        # several filesystem probes, and it was done twice per scene —
        # once for this pre-flight validation and again when the scene
        # actually rendered. The resolved dicts are kept and reused, so
        # the identical work isn't repeated (the render path resolves on
        # demand if an entry is ever missing).
        resolved_scenes: Dict[int, Dict[str, Any]] = {}
        for index, scene in enumerate(scenes):
            sd = self._scene_for_render(
                scene, timeline, str(ctx.get("images_folder") or "")
            )
            resolved_scenes[index] = sd
            img = str(sd.get("image_path") or sd.get("image") or "")
            if not img or not Path(img).is_file():
                missing_images.append(
                    f"scene {sd.get('scene_number')}: {img or '(empty)'}"
                )
        if missing_images:
            return {
                "success": False,
                "error": "missing scene image(s) before render:\n"
                + "\n".join(missing_images),
            }

        progress_cb = self._progress_forwarder(ctx)

        def _render_one(index: int, scene: Dict[str, Any]) -> Dict[str, Any]:
            scene_dict = resolved_scenes.get(index) or self._scene_for_render(
                scene, timeline, str(ctx.get("images_folder") or "")
            )
            out_path = segments_dir / f"scene_{index:03d}.mp4"
            expected_duration = float(scene_dict.get("duration") or 8.0)
            # RESUMABILITY (v3.2.1): if a previous run already rendered this
            # exact segment (same path, matching duration within 0.1s), skip
            # re-rendering it. Lets a crashed/interrupted render continue
            # from where it left off instead of starting over.
            if out_path.exists():
                existing_dur = self._probe_duration_seconds(out_path)
                if (
                    existing_dur is not None
                    and abs(existing_dur - expected_duration) < 0.1
                ):
                    return {
                        "index": index,
                        "success": True,
                        "path": str(out_path),
                        "duration": expected_duration,
                        "resumed": True,
                    }
            anim_filter = ""
            if animation_engine is not None:
                anim = animation_engine.get_zoompan_filter(
                    str(scene_dict.get("animation_type") or "ken_burns"),
                    expected_duration,
                    intensity=str(scene_dict.get("animation_intensity") or "medium"),
                )
                if anim.get("success"):
                    anim_filter = str(
                        (anim.get("data") or {}).get("filter_string") or ""
                    )
            rendered = engine.render_scene_to_video(
                scene_dict,
                anim_filter,
                grade_filter,
                out_path,
                preset=preset,
                grade_extras=grade_extras,
                progress_callback=progress_cb,
            )
            if not rendered.get("success"):
                return {
                    "index": index,
                    "success": False,
                    "error": f"scene {index} render failed: {rendered.get('error')}",
                }
            seg_path = _pick_path(
                rendered.get("data") or {}, "output_path", "video_path"
            ) or str(out_path)
            return {
                "index": index,
                "success": True,
                "path": seg_path,
                "duration": expected_duration,
                "resumed": False,
            }

        # PERFORMANCE (v3.2.1): scene renders are independent ffmpeg
        # subprocess calls (no shared state), so they can run concurrently.
        # Order is restored afterward from each result's index — the final
        # segment list is never reordered, only the rendering itself is
        # parallelized. Capped at 4 workers to avoid saturating a shared
        # hardware encoder (h264_qsv etc.) with too many simultaneous jobs.
        # PERFORMANCE (v3.2.2): raised from 4 to 6 — the 3.2.1 render log
        # confirmed 2 simultaneous h264_qsv jobs completed cleanly with no
        # contention issues, so a higher cap is safe to try. Still capped
        # (not unlimited) to avoid overloading a shared hardware encoder on
        # weaker machines; each job is still one ffmpeg subprocess.
        # FEATURE (v3.2.4): for a long render (many scenes over hours),
        # sustained load can cause real slowdown mid-render — thermal
        # throttling on a laptop, or contention if other work starts on
        # the same machine. Scenes now render in BATCHES instead of all
        # at once; each batch's actual throughput (seconds/scene) is
        # compared against the first batch's baseline, and if a later
        # batch is meaningfully slower, concurrency is reduced for
        # subsequent batches to ease load — rather than continuing to
        # push the same worker count into a machine that's struggling.
        max_workers = max(1, min(6, (os.cpu_count() or 2)))
        current_workers = max_workers
        results: Dict[int, Dict[str, Any]] = {}
        remaining = list(enumerate(scenes))
        baseline_rate: Optional[float] = None
        while remaining:
            batch = remaining[:current_workers]
            remaining = remaining[current_workers:]
            batch_started = time.perf_counter()
            outcome: Optional[Dict[str, Any]] = None
            with ThreadPoolExecutor(max_workers=current_workers) as pool:
                futures = {
                    pool.submit(_render_one, i, s): i for i, s in batch
                }
                for future in as_completed(futures):
                    # PHASE 9 (safe cancellation / thread cleanup): the
                    # queued-but-unstarted scenes are cancelled and the
                    # loop breaks, so the pool shuts down as soon as the
                    # in-flight ffmpeg jobs finish instead of rendering
                    # the whole remaining batch first. Returning from
                    # inside the `with` waited for all of them.
                    if self._cancel_requested:
                        outcome = {
                            "success": False, "cancelled": True,
                            "error": "pipeline cancelled during render",
                        }
                    elif outcome is None:
                        res = future.result()
                        results[res["index"]] = res
                        if not res["success"]:
                            outcome = {"success": False, "error": res["error"]}
                    if outcome is not None:
                        for pending in futures:
                            pending.cancel()
                        break
            if outcome is not None:
                return outcome
            batch_rate = (time.perf_counter() - batch_started) / max(1, len(batch))
            baseline_rate, current_workers = self._adjust_workers_for_slowdown(
                current_workers, baseline_rate, batch_rate,
            )

        for index in range(len(scenes)):
            res = results[index]
            segments.append({"path": res["path"], "duration": res["duration"]})
        if io_segments.get("outro"):
            segments.append(
                {"path": io_segments["outro"]["path"],
                 "duration": io_segments["outro"]["duration"] or 20.0}
            )

        self._transition(RenderState.EXPORTING, "join+mux")
        joined_path = (
            Path(str(ctx["project_folder"])) / "output" / "joined.mp4"
        )
        if ensure_directory(joined_path.parent) is None:
            return {
                "success": False,
                "error": f"cannot create output folder: {joined_path.parent}",
            }
        joined = engine.join_segments_with_transitions(
            segments, timeline, joined_path, preset=preset,
            progress_callback=progress_cb,
        )
        if not joined.get("success"):
            return joined
        ctx["joined_output"] = str(joined_path)
        # DISK CLEANUP (v3.2.1): once segments are successfully joined into
        # one file, the individual per-scene temp files are no longer
        # needed. For long, many-scene projects these can add up to a lot
        # of disk space — remove them now rather than waiting until the
        # whole project is deleted. Intro/outro files are left alone (kept
        # for potential resume/re-run reuse); only the per-scene renders
        # are cleaned up here.
        for seg in segments:
            seg_path = Path(str(seg.get("path") or ""))
            if seg_path.name.startswith("scene_"):
                # PHASE 9: safe_unlink tolerates a segment another
                # process already removed and never raises after a
                # render that otherwise succeeded.
                safe_unlink(seg_path)
        return {"success": True, "data": {"segments": len(segments)}}

    def _adjust_workers_for_slowdown(
        self,
        current_workers: int,
        baseline_rate: Optional[float],
        batch_rate: float,
    ) -> tuple:
        """Decide next batch's worker count from observed render speed.

        FEATURE (v3.2.4): pure decision logic (no I/O), kept separate so
        it can be tested deterministically without real hardware/thermal
        conditions. First batch always sets the baseline. A later batch
        running at least 40% slower than baseline triggers a one-step
        concurrency reduction (floor of 1) — eases load on a machine
        that's genuinely struggling (thermal throttling, background
        contention) rather than continuing to push the same worker count.
        Never increases workers back up automatically (a render that's
        already slowed down once is treated conservatively for the rest
        of that run).
        """
        if baseline_rate is None:
            return batch_rate, current_workers
        slowdown_threshold = 1.4  # 40% slower than baseline
        if batch_rate > baseline_rate * slowdown_threshold and current_workers > 1:
            new_workers = max(1, current_workers - 1)
            self.log.warning(
                "Scene render slowdown detected (%.2fs/scene vs %.2fs/scene "
                "baseline) — reducing concurrent renders from %d to %d",
                batch_rate, baseline_rate, current_workers, new_workers,
            )
            return baseline_rate, new_workers
        return baseline_rate, current_workers

    def _probe_duration_seconds(self, path: Path) -> Optional[float]:
        """ffprobe a media file's duration; None on any failure.

        Used by the resumable-render check (v3.2.1) to confirm an
        existing segment file from a prior/interrupted run is actually
        complete and matches the expected duration, before trusting it
        and skipping re-render.
        """
        ffprobe = None
        if self.hardware is not None and hasattr(self.hardware, "find_ffprobe"):
            ffprobe = self.hardware.find_ffprobe()
        if not ffprobe:
            return None
        try:
            proc = subprocess.run(
                [
                    str(ffprobe), "-v", "quiet",
                    "-show_entries", "format=duration",
                    "-print_format", "json", str(path),
                ],
                capture_output=True, text=True, timeout=15,
            )
            data = json.loads(proc.stdout or "{}")
            return float((data.get("format") or {}).get("duration"))
        except Exception:  # noqa: BLE001 - best-effort only
            return None

    def _scene_for_render(
        self,
        scene: Dict[str, Any],
        timeline: Dict[str, Any],
        images_root: str = "",
    ) -> Dict[str, Any]:
        """Scene dict for render_scene_to_video (image path resolution).

        Prefers the processed 1920x1080 asset; falls back to the raw
        scene image path (image_processor marks assets by original
        file path - column resolved via PRAGMA to survive schema drift).
        """
        raw = str(scene.get("image_file_path") or "")
        if not raw and images_root:
            # 3.1.0 self-heal: re-derive from the stored filename
            # (project moved / NULL slipped through an older run).
            # v3.2.18: format-tolerant too, same as the main lookup.
            raw = self._resolve_image_path(
                str(images_root), str(scene.get("image_filename") or "")
            )
        processed = self._processed_image_for(raw)
        timeline_scene = self._timeline_scene_for(
            timeline, int(scene.get("scene_number") or 0)
        )
        duration = float(
            scene.get("duration")
            or (timeline_scene or {}).get("duration")
            or 8.0
        )
        return {
            "id": scene.get("id"),
            "scene_number": scene.get("scene_number"),
            "image_path": processed or raw,
            "image": raw,
            "duration": duration,
            "animation_type": scene.get("animation_type")
            or (timeline_scene or {}).get("animation_type")
            or "ken_burns",
            "animation_intensity": scene.get("animation_intensity") or "medium",
        }

    def _processed_image_for(self, raw_path: str) -> Optional[str]:
        if not raw_path:
            return None
        column = self._image_assets_column()
        if not column:  # schema lacks a usable path column
            return None
        # D2a: resolve the path column via PRAGMA once (cached) instead
        # of try/erroring between candidate names — the failed probe
        # made database_service log a misleading ERROR per scene.
        try:
            row = self.db.db.fetch_one(
                "SELECT processed_file_path FROM image_assets"
                f" WHERE {column} = ? AND processed_file_path IS NOT NULL",
                (raw_path,),
            )
        except Exception:  # noqa: BLE001 - schema drift tolerated
            return None
        if row and row.get("processed_file_path"):
            candidate = Path(str(row["processed_file_path"]))
            if candidate.exists():
                return str(candidate)
        return None

    def _image_assets_column(self) -> Optional[str]:
        """Path column of image_assets, resolved once via PRAGMA.

        Caches both positive and negative outcomes in
        self._assets_col_cache ("" marks 'checked, none usable').
        """
        cache = getattr(self, "_assets_col_cache", None)
        if cache is not None:
            return cache or None
        names: set = set()
        try:
            rows = self.db.db.fetch_all("PRAGMA table_info(image_assets)")
            names = {str(r.get("name")) for r in (rows or [])}
        except Exception:  # noqa: BLE001 - PRAGMA never expected to fail
            names = set()
        column = ""
        if "processed_file_path" in names:
            for candidate in ("file_path", "original_file_path"):
                if candidate in names:
                    column = candidate
                    break
        self._assets_col_cache = column
        return column or None

    def _timeline_scene_for(
        self, timeline: Dict[str, Any], scene_number: int
    ) -> Optional[Dict[str, Any]]:
        # PERFORMANCE (PHASE 8): the per-scene linear scan made resolving
        # a whole project quadratic in scene count. The lookup table is
        # built once and held on the orchestrator (NOT inside the
        # timeline dict, which gets serialized to timeline_json — an
        # extra key there would change what existing projects store).
        # It is rebuilt whenever a different scenes list is seen, so a
        # rebuilt/edited timeline is never answered from a stale index.
        scenes = (timeline or {}).get("scenes") or []
        cached = self._timeline_index_cache
        if cached is None or cached[0] is not scenes or cached[1] != len(scenes):
            index: Dict[int, Dict[str, Any]] = {}
            for scene in scenes:
                try:
                    index.setdefault(int(scene.get("scene_number") or 0), scene)
                except (TypeError, ValueError):
                    continue
            cached = (scenes, len(scenes), index)
            self._timeline_index_cache = cached
        return cached[2].get(scene_number)

    def _stage_burn_subtitles(self, engine: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        srt = ctx.get("srt_path")
        joined = ctx.get("joined_output")
        if not srt or not Path(str(srt)).exists():
            ctx["final_output"] = joined
            return {"success": True, "data": {"skipped": "no srt"}}
        project = self.db.db.fetch_one(
            "SELECT has_subtitles, default_subtitle_style, title FROM projects"
            " WHERE id = ?",
            (ctx["project_id"],),
        )
        if project and not int(project.get("has_subtitles") or 0):
            ctx["final_output"] = joined
            return {"success": True, "data": {"skipped": "has_subtitles=0"}}
        final_path = self._final_output_path(ctx)
        style = str((project or {}).get("default_subtitle_style") or "word_by_word")
        burned = engine.burn_subtitles(
            str(joined), str(srt), style, final_path,
            two_pass=bool(ctx.get("two_pass_export")),
        )
        if burned.get("success"):
            ctx["final_output"] = str(final_path)
            return burned
        # Burn failure keeps the joined (unsubtitled) video as output.
        ctx["warnings"].append(
            f"subtitle burn failed; shipping unsubtitled video: {burned.get('error')}"
        )
        ctx["final_output"] = joined
        return {"success": True, "data": {"fell_back_to_joined": True}}

    def _final_output_path(self, ctx: Dict[str, Any]) -> Path:
        project = self.db.db.fetch_one(
            "SELECT title FROM projects WHERE id = ?", (ctx["project_id"],)
        )
        title = str((project or {}).get("title") or "video")
        safe_title = "".join(
            c if c.isalnum() or c in "-_ " else "" for c in title
        ).strip().replace(" ", "_") or "video"
        preset = str(ctx.get("export_preset") or "youtube_1080p")
        date = utc_now_str().split(" ")[0]
        name = f"{safe_title}_{preset}_{date}.mp4"
        out_dir = Path(str(ctx["project_folder"])) / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / name

    def _stage_verify(self, engine: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        output = ctx.get("final_output")
        if not output:
            return {"success": False, "error": "no output to verify"}
        expected = float(
            (ctx.get("timeline") or {}).get("total_duration")
            or ctx.get("narration_duration")
            or 0.0
        )
        if expected <= 0.0:
            path = Path(str(output))
            if path.exists() and path.stat().st_size > 0:
                result = {
                    "success": True,
                    "data": {"verified": False,
                             "reason": "no expected duration available"},
                }
            else:
                return {"success": False, "error": f"output missing: {output}"}
        else:
            result = engine.verify_output(str(output), expected)
        if result.get("success"):
            self._export_youtube_extras(ctx)
            self._log_storage_summary(ctx)
        return result

    def _log_storage_summary(self, ctx: Dict[str, Any]) -> None:
        """Log final output size and remaining disk space (v3.2.4).

        Best-effort only — never raises. Useful after a long render to
        see at a glance what it actually cost in disk space, and how
        much room is left for the next one.
        """
        try:
            output = Path(str(ctx.get("final_output") or ""))
            output_mb = (
                output.stat().st_size / 1024 / 1024 if output.exists() else 0.0
            )
            free_mb = 0.0
            try:
                free_mb = shutil.disk_usage(
                    str(ctx.get("project_folder") or ".")
                ).free / 1024 / 1024
            except OSError:
                pass
            self.log.info(
                "Storage summary: final video ~%.1f MB, %.0f MB free on disk",
                output_mb, free_mb,
            )
        except Exception:  # noqa: BLE001 - never fail the pipeline over this
            pass

    def _export_youtube_extras(self, ctx: Dict[str, Any]) -> None:
        """Write the SRT + metadata.txt into a dedicated subfolder (v3.2.6).

        FEATURE (v3.2.3): for actual YouTube use, two things matter beyond
        the video file itself:
          - A standalone .srt: YouTube's own uploaded-caption track is
            searchable/translatable/toggleable, unlike burned-in text.
          - A ready-to-paste description with auto-generated chapter
            timestamps — the timing data already exists (computed during
            timeline build for burned-in chapters), this just also writes
            it out in the format YouTube's description box expects.
        Best-effort only: never fails the pipeline if either step can't
        complete (e.g. missing timeline data on an older/partial run).

        BUGFIX (v3.2.6): v3.2.5 tried fixing the doubled-subtitle problem
        by renaming the sidecar file to "<video>_captions.srt" instead of
        matching the video's exact name — but user screenshots confirmed
        it STILL got auto-loaded and doubled. Some players (confirmed:
        KMPlayer) match more loosely than an exact basename — e.g. any
        .srt whose name starts with the video's name, or simply any .srt
        present in the same folder — so a same-folder rename was never
        going to be reliable. The only fix that works regardless of a
        given player's matching heuristic is to not be in the same folder
        as the video at all. Both files now go in a "youtube_upload"
        subfolder next to the output — never visible to a player browsing
        for sidecar subtitles, still easy to find when you actually want
        to upload them.
        """
        try:
            output = Path(str(ctx.get("final_output") or ""))
            if not output.exists():
                return
            extras_dir = output.parent / "youtube_upload"
            extras_dir.mkdir(parents=True, exist_ok=True)
            srt_path = ctx.get("srt_path")
            if srt_path and Path(str(srt_path)).exists():
                dest = extras_dir / f"{output.stem}.srt"
                shutil.copyfile(str(srt_path), str(dest))

            timeline = ctx.get("timeline") or {}
            chapters_text = str(timeline.get("youtube_chapters_text") or "").strip()
            project = self.db.db.fetch_one(
                "SELECT title FROM projects WHERE id = ?", (ctx["project_id"],)
            )
            title = str((project or {}).get("title") or output.stem)
            lines = [
                f"Suggested title: {title}",
                "",
                "Description:",
                f"{title} — watch to the end for the full story.",
                "",
            ]
            if chapters_text:
                lines += ["Chapters:", chapters_text, ""]
            lines += [
                "(Auto-generated from your script — edit before publishing.)",
            ]
            meta_path = extras_dir / f"{output.stem}_youtube_metadata.txt"
            atomic_write_text(meta_path, "\n".join(lines))
        except Exception as exc:  # noqa: BLE001 - never fail the pipeline
            self.log.warning("YouTube extras export skipped: %s", exc)

    def _stage_thumbnails(self, generator: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        return generator.auto_generate_for_project(ctx["project_id"])

    # ------------------------------------------------------------------
    # Stage 17: Google Drive backup (D.7 - optional, self-skipping)
    # ------------------------------------------------------------------
    def _stage_drive_upload(self, engine: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
        output = _pick_path(ctx, "final_output")
        if not output:
            return {
                "success": True,
                "data": {"skipped": "no rendered file to upload"},
            }
        return engine.upload_final_render(
            output,
            project_id=ctx.get("project_id"),
            title=ctx.get("project_title"),
        )

    def _apply_target_duration(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Correct narration speed to fit a requested target duration.

        FEATURE (v3.2.16): called once, right after the "tts" stage's
        FIRST (normal-speed) pass. Compares the real measured duration
        against the target, and if a real correction is worth it (more
        than 2% off — not worth doubling TTS time to fix a rounding-
        level difference), sets ctx["speed_multiplier"] and re-invokes
        the SAME "tts" stage through the normal _execute_stage()
        mechanism — the safest way to trigger regeneration, since it's
        the exact same well-tested dispatch every other stage uses, not
        new bespoke re-run logic.
        """
        started = time.perf_counter()
        target = float(ctx.get("target_duration_seconds") or 0.0)
        natural = float(ctx.get("narration_duration") or 0.0)
        calc = self.compute_speed_for_target_duration(natural, target)
        ctx["target_duration_result"] = {
            "target_seconds": target, "natural_seconds": natural, **calc,
        }
        within_tolerance = (
            natural > 0 and target > 0
            and abs(natural - target) / max(target, 1.0) < 0.02
        )
        no_target_set = target <= 0
        if no_target_set or within_tolerance or calc["speed"] == 1.0:
            if no_target_set:
                reason = "no target duration set"
            elif within_tolerance:
                reason = "already within 2% of target, no correction needed"
            else:
                reason = "computed speed is already 1.0x, nothing to change"
            return {
                "stage": "target_duration_fit", "status": "skipped",
                "reason": reason,
                "duration_ms": _ms(started),
            }
        self.log.info(
            "Target duration fit: natural=%.1fs target=%.1fs -> "
            "regenerating narration at %.2fx speed%s",
            natural, target, calc["speed"],
            " (clamped — target not fully reachable at natural speech "
            "rates)" if calc["clamped"] else "",
        )
        ctx["speed_multiplier"] = calc["speed"]
        regen = self._execute_stage("tts", ctx)
        ctx["target_duration_result"]["achieved_seconds"] = float(
            ctx.get("narration_duration") or 0.0
        )
        return {
            "stage": "target_duration_fit",
            "status": "completed" if regen["status"] not in ("failed", "cancelled")
            else regen["status"],
            "error": regen.get("error"),
            "duration_ms": _ms(started),
        }

    def _emit_overall_progress(
        self, ctx: Dict[str, Any], stage_name: str, pipeline_started: float
    ) -> None:
        """Log/publish overall %-complete and ETA for the whole render.

        FEATURE (v3.2.4): blends the pre-render estimate with real
        elapsed time so far — as actual stage timings come in, the ETA
        becomes more accurate than the static pre-render guess alone.
        Best-effort only; never raises (a progress display glitch must
        never fail the actual render).
        """
        try:
            estimate = ctx.get("render_estimate") or {}
            total_est = float(estimate.get("estimated_seconds") or 0.0)
            if total_est <= 0:
                return
            elapsed = time.perf_counter() - pipeline_started
            pct = min(99.0, max(0.0, (elapsed / total_est) * 100.0))
            remaining = max(0.0, total_est - elapsed)
            self.log.info(
                "Overall progress: ~%.0f%% complete after stage '%s' "
                "(elapsed %.0fs, ETA ~%.0fs remaining, estimate is "
                "approximate)",
                pct, stage_name, elapsed, remaining,
            )
            self.event_bus.publish(
                "pipeline.overall_progress",
                {
                    "project_id": ctx.get("project_id"),
                    "stage": stage_name,
                    "percent": round(pct, 1),
                    "elapsed_seconds": round(elapsed, 1),
                    "eta_seconds": round(remaining, 1),
                },
            )
        except Exception:  # noqa: BLE001 - progress display is never critical
            pass

    # ------------------------------------------------------------------
    # Finalization + helpers
    # ------------------------------------------------------------------
    def _finalize_success(self, ctx: Dict[str, Any]) -> None:
        self._transition(RenderState.COMPLETE, "pipeline finished")
        self.db.db.execute(
            "UPDATE projects SET status = 'completed',"
            " render_count = COALESCE(render_count, 0) + 1,"
            " last_render_at = ?, last_render_output_path = ?, updated_at = ?"
            " WHERE id = ?",
            (utc_now_str(), ctx.get("final_output"), utc_now_str(),
             ctx["project_id"]),
        )

    def _progress_forwarder(self, ctx: Dict[str, Any]) -> Callable[..., None]:
        """Forward module progress callbacks onto the event bus.

        Signature mirrors export_engine.monitor_ffmpeg_progress, which
        invokes callback(progress, fps, eta_seconds) with three
        positional floats. D2a hotfix: the Windows D.3 smoke (first run
        against REAL ffmpeg, which emits frame=/fps= lines) exposed
        that the previous single-arg forwarder crashed the export
        stage; POSIX fakes emit no progress lines so tests never
        invoked it. All three args default so any call shape works.
        """

        def _forward(
            progress: Any = 0.0, fps: Any = 0.0, eta: Any = 0.0
        ) -> None:
            self.event_bus.publish(
                "pipeline.render_progress",
                {
                    "project_id": ctx.get("project_id"),
                    "progress": progress,
                    "fps": fps,
                    "eta_seconds": eta,
                },
            )

        return _forward
