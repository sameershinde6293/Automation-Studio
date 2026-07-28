#!/usr/bin/env python3
"""Autopilot entry point (D.2): boots the app and exposes the CLI.

Wires the pieces that have no business knowing about each other:

* finds the project root (dev checkout or PyInstaller-frozen exe dir),
* reads config/app_settings.json to build the production
  ServiceContainer,
* runs first-launch license initialization,
* dispatches subcommands to core.core_engine.CoreEngine -
  the ONLY pipeline orchestrator (RULE 1: modules never import each
  other; wiring lives in core_engine/main only).

CLI overview (all commands print concise status; exit 0 ok / 1 fail /
2 usage):

    python main.py render --script script.txt [--images DIR]
                          [--project-folder DIR] [--title T]
                          [--preset P] [--profile REF] [--quality-gate]
                          [--skip-license]
    python main.py render-project PROJECT_ID [--preset P]
                          [--quality-gate] [--skip-license]
    python main.py check PROJECT_ID
    python main.py batch-add [--project ID | --folder DIR]
    python main.py batch
    python main.py modules
    python main.py license [--activate KEY]
    python main.py ui          (D.4 seam: launches the PyQt shell)

The D.4 PyQt GUI (ui/) and the D.5 PyInstaller build both call into
these same helpers, so CLI/GUI/frozen paths share exactly one wiring.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.core_engine import CoreEngine
from core.license_manager import LicenseManager
from core.service_container import ServiceContainer

APP_NAME = "Autopilot"
APP_VERSION = "3.1.0"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


# ----------------------------------------------------------------------
# Bootstrapping
# ----------------------------------------------------------------------
def find_project_root() -> Path:
    """Locate the application root in dev or frozen (PyInstaller) mode.

    Dev: this main.py's folder. Frozen ONEDIR (D.5): PyInstaller 6
    places ALL bundled datas under `<exe folder>/_internal/` — config/,
    database/schema.sql, assets land there while runtime folders
    (database/*.db, logs/, temp/) are created inside it too, so the
    root is `_internal` when present. A `config/` next to the exe
    (custom overrides / older layouts) still wins when it exists.
    """
    if getattr(sys, "frozen", False):  # PyInstaller onefile/onedir
        exe_dir = Path(sys.executable).resolve().parent
        internal = exe_dir / "_internal"
        if (internal / "config").is_dir() and not (exe_dir / "config").is_dir():
            return internal
        return exe_dir
    return Path(__file__).resolve().parent


def load_app_settings(project_root: Path) -> Dict[str, Any]:
    """Bootstrap-read of app_settings.json (pre-container, RULE 8)."""
    path = project_root / "config" / "app_settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError) as exc:
        print(f"[boot] warning: app_settings unreadable ({exc}); defaults")
    return {}


def _prepend_engine_dir_to_path(project_root: Path) -> Optional[Path]:
    """Put the bundled engines/ffmpeg dir on PATH (idempotent).

    Several libraries (pydub, and any subprocess-by-name call) look for
    ``ffmpeg``/``ffprobe`` on PATH instead of using HardwareService's
    configured hint. The portable app bundles both beside the config;
    prepending that folder makes them visible process-wide. On Windows
    CreateProcess/appends .exe automatically (ffmpeg.exe), so the same
    code path covers dev checkouts and the frozen exe.
    """
    engines_dir = project_root / "engines" / "ffmpeg"
    if not engines_dir.is_dir():
        return None
    current = os.environ.get("PATH", "")
    entry = str(engines_dir)
    if entry not in current.split(os.pathsep):
        os.environ["PATH"] = entry + os.pathsep + current
    return engines_dir


def build_container(project_root: Path) -> ServiceContainer:
    """Create the production service container for this app root."""
    _prepend_engine_dir_to_path(project_root)
    settings = load_app_settings(project_root)
    root = project_root

    def _resolve(value: Optional[str], fallback: str) -> str:
        raw = Path(str(value or fallback))
        return str(raw if raw.is_absolute() else root / raw)

    for folder_key, fallback in (
        ("temp_folder_path", "temp"),
        ("projects_folder_path", "projects"),
        ("cache_folder_path", "cache"),
        ("log_folder_path", "logs"),
    ):
        Path(_resolve(settings.get(folder_key), fallback)).mkdir(
            parents=True, exist_ok=True
        )
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "database").mkdir(parents=True, exist_ok=True)

    app_config = {
        "database_path": _resolve(
            settings.get("database_path"), "database/autopilot.db"
        ),
        "schema_path": str(root / "database" / "schema.sql"),
        "config_folder": str(root / "config"),
        "cache_folder": _resolve(settings.get("cache_folder_path"), "cache"),
        "log_folder": _resolve(settings.get("log_folder_path"), "logs"),
        "ffmpeg_path": _resolve(
            settings.get("ffmpeg_path"), "engines/ffmpeg/ffmpeg"
        ),
    }
    return ServiceContainer.create_production_container(
        app_config=app_config, project_root=root
    )


def boot(project_root: Path) -> Dict[str, Any]:
    """Container + CoreEngine + license initialization (first launch)."""
    container = build_container(project_root)
    engine = CoreEngine(container)
    license_manager = LicenseManager(container)
    init = license_manager.initialize_license()
    lic_data = init.get("data") or {}
    return {
        "container": container,
        "engine": engine,
        "license": license_manager,
        "license_data": lic_data,
    }


# ----------------------------------------------------------------------
# Printing helpers (console presentation only - no logic)
# ----------------------------------------------------------------------
def _print_response(title: str, response: Dict[str, Any]) -> None:
    status = "OK" if response.get("success") else "FAIL"
    print(f"[{status}] {title}")
    for warning in response.get("warnings") or []:
        print(f"  warning: {warning}")
    if response.get("error"):
        print(f"  error: {response['error']}")


def _print_pipeline_summary(response: Dict[str, Any]) -> None:
    data = response.get("data") or {}
    print("\nPipeline summary")
    print("----------------")
    for stage in data.get("stages") or []:
        mark = {
            "completed": "[ok]", "skipped": "[--]", "warning": "[! ]",
            "failed": "[XX]", "cancelled": "[C ]",
        }.get(stage.get("status"), "[??]")
        print(f"  {mark} {stage.get('stage'):<16} ({stage.get('status')})")
    output = data.get("output_file_path")
    if output:
        print(f"\nOutput: {output}")


# ----------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------
def _lower_process_priority() -> None:
    """Lower this process's OS scheduling priority (v3.2.4, --low-priority).

    Best-effort only — never blocks or fails the render if it can't be
    applied (e.g. no permission, unsupported platform). Windows uses
    IDLE_PRIORITY_CLASS via psutil; POSIX uses a positive nice value
    (lower priority, never raises above normal even if run as root).
    """
    try:
        import psutil

        proc = psutil.Process()
        if hasattr(psutil, "IDLE_PRIORITY_CLASS"):
            proc.nice(psutil.IDLE_PRIORITY_CLASS)
        else:
            proc.nice(10)  # POSIX: higher nice value = lower priority
        print("[boot] Lowered process priority (--low-priority)")
    except Exception as exc:  # noqa: BLE001 - never fail the render over this
        print(f"[boot] Could not lower process priority (continuing anyway): {exc}")


def cmd_render(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    engine: CoreEngine = ctx["engine"]
    script = Path(str(args.script))
    if not script.exists():
        print(f"[FAIL] script not found: {script}")
        return EXIT_FAIL
    if getattr(args, "low_priority", False):
        _lower_process_priority()
    project_folder = args.project_folder or str(
        find_project_root() / "projects" / script.stem
    )
    print(f"[boot] rendering script: {script}")
    response = engine.run_script_pipeline(
        script_path=str(script),
        project_folder=project_folder,
        title=args.title,
        images_folder=args.images,
        export_preset=args.preset,
        channel_profile_id=args.profile,
        skip_stages=tuple(args.skip_stage or ()),
        enforce_license=not args.skip_license,
        quality_gate=bool(args.quality_gate),
        preview_max_scenes=getattr(args, "preview", None),
        two_pass_export=bool(getattr(args, "two_pass", False)),
        target_duration_seconds=getattr(args, "target_duration", None),
    )
    _print_response("script pipeline", response)
    _print_pipeline_summary(response)
    tdr = (response.get("data") or {}).get("target_duration_result")
    if tdr:
        if tdr.get("clamped"):
            print(
                f"[WARN] Target duration ({tdr['target_seconds']:.0f}s) needed "
                f"{tdr['requested_speed']:.2f}x speed, outside the natural "
                f"0.5x-2.0x range — clamped to {tdr['speed']:.2f}x. "
                f"Achieved ~{tdr.get('achieved_seconds', tdr['projected_duration_seconds']):.0f}s "
                "instead, to avoid distorted speech."
            )
        elif "achieved_seconds" in tdr:
            print(
                f"Target duration: {tdr['target_seconds']:.0f}s requested, "
                f"{tdr['achieved_seconds']:.0f}s achieved "
                f"(speed {tdr['speed']:.2f}x)."
            )
    return EXIT_OK if response.get("success") else EXIT_FAIL


def cmd_tts_only(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """Standalone mode (v3.2.15): narration + music mix, no video at all.

    Reuses the exact skip_stages set already proven for the batch
    queue's "Audio Only" job type (v3.2.14) — verified against the real
    pipeline stage list that this correctly skips every video-producing
    stage while leaving tts/audio_mix/parse untouched.
    """
    engine: CoreEngine = ctx["engine"]
    script = Path(str(args.script))
    if not script.exists():
        print(f"[FAIL] script not found: {script}")
        return EXIT_FAIL
    project_folder = args.project_folder or str(
        find_project_root() / "projects" / script.stem
    )
    audio_only_skip = (
        "images", "intro_outro", "subtitles", "timeline",
        "export", "burn_subtitles", "verify", "thumbnails", "drive_upload",
    )
    response = engine.run_script_pipeline(
        script_path=str(script),
        project_folder=project_folder,
        title=args.title,
        enforce_license=not args.skip_license,
        skip_stages=audio_only_skip,
    )
    _print_response("tts-only pipeline", response)
    if response.get("success"):
        mix_path = Path(project_folder) / "audio" / "final_mix.wav"
        print(f"Narration + music mix: {mix_path}")
    return EXIT_OK if response.get("success") else EXIT_FAIL


def cmd_subtitles_only(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """Standalone mode (v3.2.15): burn subtitles onto an existing video.

    Two ways to get an SRT: pass one directly with --srt, or pass
    --script to generate one from a script's text.

    HONEST LIMITATION on the --script path: this is a standalone tool
    with no TTS-generated word timestamps to sync against (unlike the
    full pipeline, which times subtitles against real per-word audio
    timing). Instead it evenly distributes each line's screen time
    across the video's actual duration (measured via ffprobe), weighted
    by each line's word count. This is a reasonable approximation for
    narration-paced content, but it is NOT the same word-accurate sync
    the full render pipeline produces — if you need that precision, use
    the full render pipeline (which generates its own subtitles from
    real TTS timestamps) instead of this standalone tool.
    """
    engine: CoreEngine = ctx["engine"]
    video = Path(str(args.video))
    if not video.exists():
        print(f"[FAIL] video not found: {video}")
        return EXIT_FAIL
    from modules.subtitle_engine import SubtitleEngine
    subtitle_engine = SubtitleEngine(ctx["container"])

    srt_path: Optional[Path] = None
    if args.srt:
        srt_path = Path(str(args.srt))
        if not srt_path.exists():
            print(f"[FAIL] SRT not found: {srt_path}")
            return EXIT_FAIL
    elif args.script:
        script_path = Path(str(args.script))
        if not script_path.exists():
            print(f"[FAIL] script not found: {script_path}")
            return EXIT_FAIL
        generated = engine.generate_srt_from_script_evenly(
            str(script_path), str(video)
        )
        if not generated.get("success"):
            print(f"[FAIL] could not generate subtitles: {generated.get('error')}")
            return EXIT_FAIL
        srt_path = Path(str((generated.get("data") or {}).get("srt_path")))
    else:
        print("[FAIL] provide either --srt or --script")
        return EXIT_FAIL

    output = Path(str(args.output)) if args.output else (
        video.parent / f"{video.stem}_subtitled{video.suffix}"
    )
    result = subtitle_engine.burn_subtitles(
        str(video), str(srt_path), str(args.style), str(output)
    )
    _print_response("subtitles-only", result)
    if result.get("success"):
        print(f"Output: {output}")
    return EXIT_OK if result.get("success") else EXIT_FAIL


def cmd_automate(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """Standalone mode (v3.2.15): one zip -> render, or an honest report.

    Deliberately built from pieces that already existed and were
    already tested, not new guessing logic:
      1. Extracts every file from the zip to a temp folder.
      2. Runs it through the SAME import classify/stage pipeline the
         Studio page's "Stage into project" button already uses
         (UiViewModel.import_plan / apply_import) — script, images,
         audio, video are auto-detected by extension, same as always.
      3. Checks the ONE thing that actually determines whether a render
         can proceed: is there a script, AND is there at least one
         image? If either is missing, this STOPS and tells you exactly
         what's missing — it never guesses or renders a broken video.
      4. If both are present, runs the normal full render pipeline —
         no different from providing the files separately.

    HONEST SCOPE NOTE: "automatically follow instructions like image
    duration, transitions, pauses" is NOT new work here — the script
    parser already reads all of that (//DURATION, //IMAGE, transition
    directives, [PAUSE] tags, etc., built over earlier releases this
    session). What THIS command adds is specifically the zip-in,
    one-command convenience and the completeness check. It does not
    add any new script-format capability beyond what the parser
    already supports.
    """
    import shutil
    import tempfile
    import zipfile

    engine: CoreEngine = ctx["engine"]
    zip_path = Path(str(args.zip))
    if not zip_path.exists():
        print(f"[FAIL] zip not found: {zip_path}")
        return EXIT_FAIL

    extract_dir = Path(tempfile.mkdtemp(prefix="autopilot_automate_"))
    try:
        with zipfile.ZipFile(str(zip_path)) as bundle:
            bundle.extractall(str(extract_dir))
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"[FAIL] could not open zip: {exc}")
        return EXIT_FAIL

    extracted_paths = [
        str(p) for p in extract_dir.rglob("*")
        if p.is_file() and not p.name.startswith((".", "__"))
    ]
    if not extracted_paths:
        print("[FAIL] zip is empty")
        shutil.rmtree(extract_dir, ignore_errors=True)
        return EXIT_FAIL

    from ui.viewmodel import UiViewModel
    vm = UiViewModel(ctx)
    plan = vm.import_plan(extracted_paths)
    project_folder = args.project_folder or str(
        find_project_root() / "projects" / zip_path.stem
    )
    staged = vm.apply_import(plan, project_folder)
    shutil.rmtree(extract_dir, ignore_errors=True)

    missing = []
    if not staged.get("script_path"):
        missing.append(
            "no script file (.txt/.json/.csv/.docx/.pdf) found in the zip"
        )
    if not staged.get("images_folder"):
        missing.append("no image files (.jpg/.jpeg/.png) found in the zip")
    if missing:
        print("[FAIL] Cannot automate — the zip is incomplete:")
        for item in missing:
            print(f"  - {item}")
        print("Add the missing file(s) to the zip and try again.")
        return EXIT_FAIL

    print(
        f"[OK] Zip complete — {staged['copied']} file(s) staged. "
        f"Starting render..."
    )
    response = engine.run_script_pipeline(
        script_path=staged["script_path"],
        images_folder=staged["images_folder"],
        project_folder=project_folder,
        title=args.title,
        enforce_license=not args.skip_license,
    )
    _print_response("automate pipeline", response)
    _print_pipeline_summary(response)
    return EXIT_OK if response.get("success") else EXIT_FAIL


def cmd_render_project(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    engine: CoreEngine = ctx["engine"]
    print(f"[boot] rendering project: {args.project_id}")
    response = engine.run_project_pipeline(
        str(args.project_id),
        export_preset=args.preset,
        skip_stages=tuple(args.skip_stage or ()),
        enforce_license=not args.skip_license,
        quality_gate=bool(args.quality_gate),
    )
    _print_response("project pipeline", response)
    _print_pipeline_summary(response)
    return EXIT_OK if response.get("success") else EXIT_FAIL


def cmd_check(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    engine: CoreEngine = ctx["engine"]
    checker = engine.module("quality_checker")
    if checker is None:
        print("[FAIL] quality_checker module not loaded")
        return EXIT_FAIL
    result = checker.run_full_check(str(args.project_id))
    if not result.get("success"):
        _print_response("quality check", result)
        return EXIT_FAIL
    report = checker.generate_report(result.get("data") or {})
    print(report or "(no report text)")
    data = result.get("data") or {}
    return EXIT_OK if data.get("is_render_ready") else EXIT_FAIL


def cmd_batch(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    engine: CoreEngine = ctx["engine"]
    batch = engine.module("batch_engine")
    if batch is None:
        print("[FAIL] batch_engine module not loaded")
        return EXIT_FAIL
    print("[boot] processing batch queue...")
    response = batch.process_queue(processor=engine.make_batch_processor())
    _print_response("batch queue", response)
    data = response.get("data") or {}
    print(
        f"  processed={data.get('processed')} completed="
        f"{data.get('completed')} failed={data.get('failed')}"
    )
    if data.get("stopped_early"):
        print("  note: queue stopped early")
    failed_count = int(data.get("failed") or 0)
    stopped = bool(data.get("stopped_early"))
    if not response.get("success"):
        return EXIT_FAIL
    return EXIT_FAIL if (failed_count and stopped) else EXIT_OK


def cmd_batch_add(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    engine: CoreEngine = ctx["engine"]
    batch = engine.module("batch_engine")
    if batch is None:
        print("[FAIL] batch_engine module not loaded")
        return EXIT_FAIL
    response = batch.add_to_queue(
        project_folder_path=args.folder,
        project_id=args.project,
        priority=int(args.priority),
        notes=args.notes or "",
    )
    _print_response("batch add", response)
    return EXIT_OK if response.get("success") else EXIT_FAIL


def cmd_modules(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    engine: CoreEngine = ctx["engine"]
    status = engine.get_module_status()
    data = status.get("data") or {}
    print("Module status (registry order)")
    print("------------------------------")
    report = data.get("report") or {}
    for name, info in sorted(
        report.items(), key=lambda kv: int(kv[1].get("priority") or 99)
    ):
        if info.get("loaded"):
            mark = "[ok]"
        elif not info.get("enabled"):
            mark = "[--]"
        else:
            mark = "[XX]"
        req = "required" if info.get("required") else "optional"
        error = f" ({info['error']})" if info.get("error") else ""
        print(f"  {mark} {name:<24} {req}{error}")
    plugin_status = getattr(engine, "get_plugin_status", None)
    if callable(plugin_status):  # D.8 plugin section (optional engine)
        pdata = (plugin_status() or {}).get("data") or {}
        preport = pdata.get("report") or {}
        if preport:
            print("Plugin status (alphabetical)")
            print("------------------------------")
            for name, info in sorted(preport.items()):
                if info.get("loaded"):
                    mark = "[ok]"
                elif not info.get("enabled"):
                    mark = "[--]"
                else:
                    mark = "[XX]"
                error = f" ({info['error']})" if info.get("error") else ""
                print(f"  {mark} {name:<24} plugin{error}")
    return EXIT_OK


def cmd_license(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    license_manager: LicenseManager = ctx["license"]
    if args.activate:
        response = license_manager.activate_license(str(args.activate))
        _print_response("license activation", response)
        if not response.get("success"):
            return EXIT_FAIL
    status = (license_manager.check_license().get("data") or {})
    print("License status")
    print("--------------")
    print(f"  status:         {status.get('status')}")
    print(f"  days remaining: {status.get('days_remaining')}")
    print(f"  clock tampered: {status.get('clock_tampered')}")
    print(f"  HWID:           {(ctx['license_data'] or {}).get('hwid')}")
    ok = status.get("status") in ("active", "trial")
    return EXIT_OK if ok else EXIT_FAIL


def cmd_plugin(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """D.8: list plugins or run one: plugin <name> --arg k=v ..."""
    engine: CoreEngine = ctx["engine"]
    if getattr(args, "list", False) or not getattr(args, "name", None):
        data = (engine.get_plugin_status() or {}).get("data") or {}
        report = data.get("report") or {}
        print("Plugin status (alphabetical)")
        print("------------------------------")
        if not report:
            print("  (no plugins registered in"
                  " config/plugins_config.json)")
        for name, info in sorted(report.items()):
            if info.get("loaded"):
                mark = "[ok]"
            elif not info.get("enabled"):
                mark = "[--]"
            else:
                mark = "[XX]"
            error = f" ({info['error']})" if info.get("error") else ""
            print(f"  {mark} {name:<24} plugin{error}")
        return EXIT_OK
    context: Dict[str, Any] = {}
    for pair in getattr(args, "arg", None) or []:
        if "=" not in pair:
            print(f"[FAIL] --arg must be KEY=VALUE, got: {pair!r}")
            return EXIT_USAGE
        key, value = pair.split("=", 1)
        context[key] = value
    response = engine.run_plugin(args.name, context)
    out = response.get("data") or {}
    try:
        print(json.dumps(out, indent=2, default=str))
    except (TypeError, ValueError):
        print(out)
    if response.get("error"):
        print(f"[FAIL] {response['error']}")
    return EXIT_OK if response.get("success") else EXIT_FAIL


def cmd_ui(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    print(f"[boot] {APP_NAME} {APP_VERSION} - launching UI...")
    try:
        from ui.app import launch  # D.4 seam (PyQt6 shell)
    except ImportError:
        print(
            "[--] UI needs PyQt6:  pip install -r requirements_ui.txt\n"
            "     CLI is ready:     python main.py render"
            " --script <file> --images <dir>"
        )
        return EXIT_OK
    return launch(ctx)


# ----------------------------------------------------------------------
# Argument parsing + dispatch
# ----------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autopilot",
        description=(
            f"{APP_NAME} {APP_VERSION} - offline documentary video"
            " automation (script -> narrated MP4)"
        ),
    )
    parser.add_argument("--version", action="version",
                        version=f"{APP_NAME} {APP_VERSION}")
    sub = parser.add_subparsers(dest="command")

    render = sub.add_parser("render", help="render a script file to MP4")
    render.add_argument("--script", required=True, help="script file path")
    render.add_argument("--images", help="folder with scene images")
    render.add_argument("--project-folder", help="target project folder")
    render.add_argument("--title", help="override project title")
    render.add_argument("--preset", help="export preset (from config)")
    render.add_argument("--profile", help="channel profile id/name")
    render.add_argument("--quality-gate", action="store_true",
                        help="abort on unresolved critical/error checks")
    render.add_argument("--skip-license", action="store_true",
                        help="skip license enforcement (dev/testing)")
    render.add_argument("--skip-stage", action="append",
                        help="skip a pipeline stage (repeatable)")
    render.add_argument(
        "--preview", type=int, nargs="?", const=3, default=None,
        metavar="N",
        help=(
            "quick preview: render only the first N scenes (default 3) at "
            "low resolution instead of the full project — sanity-check "
            "timing/sync/visuals in under a minute before committing to "
            "a long render"
        ),
    )
    render.add_argument(
        "--two-pass", action="store_true",
        help=(
            "higher quality-per-size final encode (roughly 2x slower for "
            "the subtitle-burn step); use for a final deliverable, not "
            "quick tests"
        ),
    )
    render.add_argument(
        "--target-duration", type=float, default=None, metavar="SECONDS",
        help=(
            "fit narration to this many seconds by adjusting TTS speed "
            "(e.g. a 5-minute script stretched to fill 600 seconds); "
            "clamped to 0.5x-2.0x speed, same range as the Speed slider"
        ),
    )
    render.add_argument(
        "--low-priority", action="store_true",
        help=(
            "lower this process's OS priority so a long render doesn't "
            "make the rest of your PC sluggish while it runs"
        ),
    )
    render.set_defaults(handler=cmd_render)

    # FEATURE (v3.2.15): standalone single-purpose modes — narration
    # only, no image/video/subtitle work. Reuses the exact skip_stages
    # set already tested and shipped for the queue's "Audio Only" job
    # type (v3.2.14) — same proven mechanism, just exposed directly.
    tts_only = sub.add_parser(
        "tts-only",
        help="text-to-speech only — narration + music mix, no video",
    )
    tts_only.add_argument("--script", required=True, help="script file path")
    tts_only.add_argument("--project-folder", help="target project folder")
    tts_only.add_argument("--title", help="override project title")
    tts_only.add_argument("--skip-license", action="store_true")
    tts_only.set_defaults(handler=cmd_tts_only)

    # FEATURE (v3.2.15): burn subtitles onto an already-finished video —
    # no re-render of anything else. Either provide an existing .srt, or
    # a script to auto-generate one from (matched to the video by
    # duration, evenly distributed — see cmd_subtitles_only's docstring
    # for the honest limitation on this timing approach).
    subs_only = sub.add_parser(
        "subtitles-only",
        help="burn subtitles onto an existing finished video",
    )
    subs_only.add_argument("--video", required=True, help="existing video file")
    subs_only.add_argument("--srt", help="existing .srt file to burn")
    subs_only.add_argument(
        "--script", help="script to generate subtitles from (if no --srt given)"
    )
    subs_only.add_argument("--style", default="bold_impact", help="subtitle style preset")
    subs_only.add_argument("--output", help="output video path")
    subs_only.set_defaults(handler=cmd_subtitles_only)

    # FEATURE (v3.2.15): Automate — one zip in, either a finished video
    # out, or an honest, specific report of what's missing. Composed
    # almost entirely from already-tested pieces (zip extraction from
    # 3.2.11, the import classify/stage pipeline that already existed)
    # rather than new guessing logic — see cmd_automate's docstring for
    # exactly what "automatic" means here and its honest limits.
    automate = sub.add_parser(
        "automate",
        help="one zip in -> render, or a report of what's missing",
    )
    automate.add_argument("--zip", required=True, help="bundle .zip file")
    automate.add_argument("--project-folder", help="target project folder")
    automate.add_argument("--title", help="override project title")
    automate.add_argument("--skip-license", action="store_true")
    automate.set_defaults(handler=cmd_automate)

    render_p = sub.add_parser("render-project", help="render a DB project")
    render_p.add_argument("project_id")
    render_p.add_argument("--preset", help="export preset (from config)")
    render_p.add_argument("--quality-gate", action="store_true")
    render_p.add_argument("--skip-license", action="store_true")
    render_p.add_argument("--skip-stage", action="append")
    render_p.set_defaults(handler=cmd_render_project)

    check = sub.add_parser("check", help="run quality checks for a project")
    check.add_argument("project_id")
    check.set_defaults(handler=cmd_check)

    batch = sub.add_parser("batch", help="process the render queue")
    batch.set_defaults(handler=cmd_batch)

    batch_add = sub.add_parser("batch-add", help="queue a project render")
    batch_add.add_argument("--project", help="project id")
    batch_add.add_argument("--folder", help="project folder path")
    batch_add.add_argument("--priority", type=int, default=5)
    batch_add.add_argument("--notes", default="")
    batch_add.set_defaults(handler=cmd_batch_add)

    modules = sub.add_parser("modules", help="list module load status")
    modules.set_defaults(handler=cmd_modules)

    plugin = sub.add_parser("plugin", help="run a user plugin (D.8)")
    plugin.add_argument("name", nargs="?", help="plugin name to run")
    plugin.add_argument("--list", action="store_true",
                        help="list plugin load status")
    plugin.add_argument("--arg", action="append", default=[],
                        metavar="KEY=VALUE",
                        help="context pair for the plugin (repeatable)")
    plugin.set_defaults(handler=cmd_plugin)

    license_cmd = sub.add_parser("license", help="show license status")
    license_cmd.add_argument("--activate", metavar="KEY",
                             help="activate a license key")
    license_cmd.set_defaults(handler=cmd_license)

    ui = sub.add_parser("ui", help="launch the PyQt UI (Phase D.4)")
    ui.set_defaults(handler=cmd_ui)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK

    project_root = find_project_root()
    try:
        ctx = boot(project_root)
    except Exception as exc:  # noqa: BLE001 - boot must never traceback
        print(f"[FAIL] boot failed: {exc}")
        return EXIT_FAIL

    license_status = (ctx.get("license_data") or {}).get("status") or {}
    status_value = (
        license_status.get("status")
        if isinstance(license_status, dict)
        else license_status
    )
    print(
        f"[boot] {APP_NAME} {APP_VERSION} | root={project_root} | "
        f"license={status_value}"
    )
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    try:
        return int(handler(args, ctx))
    except KeyboardInterrupt:
        # PHASE 9 (safe cancellation): Ctrl-C during a render asks the
        # orchestrator to stop between units so in-flight work finishes
        # cleanly, instead of unwinding mid-write.
        print("\n[FAIL] interrupted by user")
        _request_cancel(ctx)
        return EXIT_FAIL
    except Exception as exc:  # noqa: BLE001 - CLI never dumps tracebacks
        print(f"[FAIL] unexpected error: {exc}")
        return EXIT_FAIL
    finally:
        # PHASE 9 (resource cleanup): release database connections (and
        # their WAL handles) opened on render worker threads, so the
        # process exits without leaving the project DB locked.
        _shutdown(ctx)


def _request_cancel(ctx: Dict[str, Any]) -> None:
    """Ask a running pipeline to stop; never raises."""
    engine = ctx.get("engine")
    if engine is None:
        return
    try:
        engine.cancel_pipeline()
    except Exception:  # noqa: BLE001 - shutdown must not mask the exit
        pass


def _shutdown(ctx: Dict[str, Any]) -> None:
    """Release process-wide resources at exit; never raises."""
    container = ctx.get("container")
    if container is None:
        return
    try:
        database = container.get("database")
        backend = getattr(database, "db", None)
        closer = getattr(backend, "close_all", None)
        if callable(closer):
            closer()
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass


if __name__ == "__main__":
    sys.exit(main())
