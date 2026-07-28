# AUTOPILOT PROJECT STATE

Last Updated: 2026-07-16 16:20:00 UTC
Current Version: 1.0.0-dev
Active Agent Session: phase-c

## CURRENT STATUS

Phase B: **COMPLETE AND WINDOWS-VERIFIED** (271 passed / 1 trivial fail / 5 skipped).
B.12.1a byte fix shipped: python fake now writes FAKEMP4DATA via write_bytes
(byte-identical to bash echo on all OS; the 1 Windows failure is resolved).

Phase C: **COMPLETE AND WINDOWS-VERIFIED** (user: 471 passed, 5 skipped, 0 failed).

Phase: **1.0.0 SHIPPED — D.8 PLUGINS + D.9 INSTALLER SHIPPED (587/5/0 both modes)**
- FULL-UI BUILD (3 fast batches, user directive: batch all,
  test at the end). NOTE: ui_specification.txt (spec File 04) is
  NOT in the workspace — building from the user's feature list +
  config/keyboard_shortcuts.json + house rules; user may paste the
  spec later for delta reconciliation.
  Batch 1 (14.UI1.zip): splash (AutopilotSplash, stepped), full
  menu bar (File/Edit/Render/View/Help) + QToolBar, both derived
  from ONE ACTION_DEFS table via viewmodel menu_model()/
  toolbar_model() (no drift possible); status-bar permanent fields
  (license/modules/plugins); shortcuts from keyboard_shortcuts.json
  (Space deferred to Batch 2 preview); single _dispatch_action
  router; theme switcher (dark/light, THEMES in ui/theme.py,
  persisted via app_settings theme, runtime re-apply + radio
  menu state). +10 headless chrome tests (test_ui_chrome.py),
  +3 Qt smoke (Windows-side). Sandbox: chrome tests 10/10 green.
  Batch 2 (15.UI2.zip): Studio page (NAV now 4: Render/Studio/
  Projects/Settings) = Import + Preview + Timeline panels in
  ui/panels/. ImportPanel: drag-and-drop anywhere + Add Files,
  classify/flag (ready/missing/duplicate/unsupported), stage into
  <project>/imports/<kind>/, auto pre-fills the render form.
  PreviewPanel: QMediaPlayer+QVideoWidget transport (play/pause
  via panel-scoped Space, seek, rate 0.5-2x, volume), Copy frame
  to clipboard, Open last render; defensive QtMultimedia import
  with honest fallback pane. TimelinePanel: read-only scene/
  dialogue tree from the DB (text_content column discovered via
  schema, candidate keys kept), estimated-durations honesty.
  viewmodel UiStudioMixin: import_plan/apply_import/preview_source/
  transport_state/timeline_model — all Qt-free. Menu/shortcut
  actions re-pointed through the single dispatcher. Fixed pins:
  Qt nav count 3->4 (Studio). +13 headless studio tests green.
  Batch 3 (16.UI3.zip): ui/dialogs/ — NewProjectDialog (slugged
  folder suggestion incl. unicode titles, validation creates the
  folder honestly), RecoveryDialog (boot-time: render_progress
  markers is_resumable=1 AND stage != completed -> resume via
  generalized _RenderWorker job=run_project_pipeline, or discard
  markers), RenderCompleteDialog (output facts, Play in Preview,
  Open folder, Upload to Drive via the D.7 seam with honest
  disable tooltip when unconfigured). viewmodel UiDialogsMixin
  (+slugify_title). launch() runs post_boot_checks via QTimer.
  +9 headless dialog tests + 3 Qt dialog smokes (Windows-side).
- D8D9a hotfix (2026-07-17, drop: Autopilot 13.D8D9a.zip): Windows
  gate caught the ONE sandbox-invisible pin — test_ui_app_qt.py
  window-stage-rows count 17 -> 18 (Qt tests skip in the sandbox;
  D.7's stage addition was pinned everywhere EXCEPT that file).
  User run: 592 passed / 1 failed (this pin) / 4 skipped.
- D.8 plugin interface + D.9 Inno installer (2026-07-17, single
  batch per speed directive, drop: Autopilot 13.D8D9.zip):
  - D.8: plugins/ folder + config/plugins_config.json registry (16th
    config file). CoreEngine.load_plugins() imports user .py files
    BY FILE PATH (spec_from_file_location) so they work identically
    dev-side and in the frozen onedir (_internal/plugins) without
    rebuilds or hiddenimports. Contract: PLUGIN_API=1 + ONE
    BaseModule subclass with run(context) -> dict; loader report is
    RULE-7 honest (missing file/syntax/no-subclass/API mismatch
    recorded, never raised); run_plugin normalizes returns, isolates
    crashes, publishes plugin.started/completed/failed. CLI: main.py
    plugin --list / plugin <name> --arg k=v; modules command gained
    a plugin section; Settings engines panel shows Plugins loaded.
    plugins/hello_autopilot.py is the shipped reference plugin.
    Deliberate: plugins are NOT pipeline stages (fixed honest v1
    order); docs/PLUGINS.md documents RULE 1 for plugin authors.
  - D.9: installer/autopilot_setup.iss (Inno Setup 6; onedir
    dist/Autopilot -> %ProgramFiles%/Autopilot, Start Menu UI entry,
    optional desktop icon, writable folders, AFTER_INSTALL ffmpeg
    reminder, uninstaller), scripts/build_installer.bat (ISCC
    discovery + dist check), docs/INSTALLER.md. tests pin sections,
    flags, GUID, and the .iss<->main.py APP_VERSION contract.
  - spec datas += plugins/ (example rides the frozen build).
  - Sandbox honesty: Inno is Windows-only; the .iss is statically
    pinned, first real compile happens on the user machine.
- D.7 Google Drive backup (2026-07-17, drop: Autopilot 12.D7.zip):
  - modules/drive_upload_engine.py (registry #20, optional,
    CAN BE DISABLED: YES). Pure REST: Drive v3 resumable-upload
    protocol over requests + service-account RS256 JWT signed with
    cryptography — zero new dependencies, frozen-exe ready.
  - Offline-first honesty (RULE 7): disabled/unconfigured/offline ->
    stage self-skips via new core_engine outcome-skip semantics
    (data.skipped -> status 'skipped' + pipeline.stage_skipped),
    mid-upload drops become warnings + persisted session state
    (cache/drive_upload_state/*.upload.json) that resumes from the
    last confirmed byte — auto on next render and via Settings.
  - Pipeline is now 18 stages (drive_upload last); viewmodel
    exposes stage_skipped events, drive_upload_status(),
    resume_drive_uploads(); Settings page gained a Google Drive
    backup panel (status + resume button). RULE 1 kept: module
    imports nothing; UI reaches it via engine.module() only.
  - config/drive_upload.json (enabled:false by default; 15 config
    files total); docs/DRIVE_UPLOAD.md setup guide; spec
    hiddenimports += drive_upload_engine (module count 20).
  - Tests: +21 (test_drive_upload_engine.py) — full REST protocol
    pinned offline via scripted localhost transport + real RSA JWT
    signature verification; suite 566 passed / 5 skipped / 0 failed
    BOTH modes (pre-D.7 sandbox baseline: 545/5/0; Windows gate
    pending). Localhost/127.0.0.1 literals exist only behind the
    drive_endpoints test override; a missed override fails tests
    loudly, production keeps googleapis defaults.
  - KNOWN: no real Drive account was contacted from the sandbox;
    the protocol is pinned against a fake server. First real upload
    needs the user's service account (docs/DRIVE_UPLOAD.md).
- D.6 UI polish (2026-07-17, drop: Autopilot 11.D6.zip):
  - ui/theme.py: full dark QSS palette (amber accent) + lazy apply_theme
    (headless-testable constant; no Qt import at module level)
  - ui/app.py restructure: left nav rail + QStackedWidget pages —
    Render (form+monitor), Projects (refresh + open-folder reveal via
    QDesktopServices), Settings (license summary + inline activation,
    engines availability panel, about). All prior widget hooks kept.
  - Data-driven form (no hardcoded lists): preset combo from
    config/export_presets.json (default flagged, None='(default
    preset)'), channel profile combo from channel_profiles table —
    passes channel_profile_id into run_script_pipeline (contract param
    existed, UI finally exposes it)
  - viewmodel +: export_presets(), channel_profiles(), engines_status()
    (ffmpeg/ffprobe/piper paths + module count, all defensive),
    activate_license() normalization (license_data cache invalidated
    so the UI refreshes itself)
  - Tests: +4 viewmodel providers (545 total, both modes green) +
    3 Qt smoke additions (nav switching, data-driven combos vs real
    config/DB, app-wide theme) — Qt tests skip in sandbox (no Qt libs),
    compile+gate clean, execute on Windows
  - KNOWN: Qt tests could not be RUN in this sandbox (libxkbcommon
    absent) — suite evidence for D.6 app code is compile + AST gates +
    Qt-free viewmodel coverage; Windows run executes the 6 Qt tests
- V1.0.0 metadata + FINAL ZIP: Autopilot 1.0.zip (2026-07-17):
  extraction-verified 541/5/0 both modes; APP_VERSION=1.0.0;
  build_manifest rewritten with the 6-gate proof + repo link
  (https://github.com/sameershinde6293/Autopilot)
- HONESTY LOG: repo build/ dir was silently deleted after 11.D5
  (unknown cause — no rm targets it; suspect pruned sandbox state);
  caught by the 1.0-zip structure diff; spec restored byte-identical
  from 11.D5 and gates re-run. Diff-verify earns its keep, again.
- User-verified on Windows (10.D4b/11.D5 lineage):
  1. pytest tests -q + Windows-fakes: 544 passed / 4 skipped / 0 failed
  2. python main.py render: all 17 stages [ok], REAL MP4
  3. python main.py ui: window opens
  4. scripts\build_exe.bat: dist\Autopilot\Autopilot.exe created
  5. frozen modules: 19/19 [ok]
  6. FROZEN RENDER: 17/17 [ok], REAL MP4 FROM THE EXE
- Repo: https://github.com/sameershinde6293/Autopilot (user-pushed)
- Final 1.0.0 metadata bump (main.py, README, build_manifest.json)
- NEXT (user-approved, batched): D.6 UI polish (dark theme, panels) →
  D.7 Google Drive → D.8 plugins → D.9 installer
- D.5 + D.6-batch-of-Phase-D (2026-07-17, drop: Autopilot 11.D5.zip), built per
  user directive "bundle, test at major milestones only":
  - build/autopilot.spec: ONEDIR COLLECT (onefile breaks find_project_root
    and the writable project root — documented in the spec header);
    hiddenimports = all 19 registry modules + 13 core modules + lazy
    third-party (pydub/thefuzz/pdfplumber/chardet/cryptography/
    soundfile/docx...); PyQt6 conditional (UI embedded when present at
    build time); tests/pytest/gui-toolkits excluded; UPX off (AV)
  - scripts/build_exe.bat (Windows one-shot: install pyinstaller →
    clean → build → self-check `Autopilot.exe modules` → guidance) +
    scripts/build_exe.sh (POSIX twin; doubles as the sandbox milestone)
  - build.bat stub replaced (now calls scripts\build_exe.bat)
  - docs/BUILD_EXE.md: layout, engines provisioning into
    dist\Autopilot\_internal\engines\ffmpeg\ (PyInstaller 6 puts ALL
    bundled datas under _internal/), run commands, limitations
  - .gitignore (build/dist/runtime/engines/local scratch) — git-ready
  - README refresh (Phase-A lie fixed: quick start, status, exe docs)
  - REAL PRODUCT FIX (found by the frozen boot, exactly why the
    milestone exists): main.find_project_root now returns
    `<exe>/_internal` when frozen+onedir (bundled datas live there);
    a user config/ override beside the exe still wins; +1 pinning test
  - SANDBOX MILESTONE PROOF (Linux ELF build): PyInstaller 6.21 built
    the bundle from the spec → frozen `modules` boot green (19/19)
    → frozen `render` ran the FULL 17-stage pipeline (16 ok + verify
    skipped) with fake engines incl. FAKE_FRAMES progress path →
    MP4 written, rc=0. On Windows the same spec produces
    dist\Autopilot\Autopilot.exe — pending user's build.
- D4b test-only hotfix (2026-07-17, drop: Autopilot 10.D4b.zip) —
  real-Windows gate with REAL ffmpeg provisioned in engines/:
  the 3 "without_ffmpeg" graceful-path tests (color_grade/export/
  subtitle burn) depended on host state (passed only when ffmpeg
  was genuinely absent). Now deterministic: each monkeypatches its
  engine's hardware.find_ffmpeg -> None, so the graceful-failure
  path is exercised on EVERY host (bundled ffmpeg or not).
  Verified 4 ways in sandbox: engines-present and engines-absent x
  POSIX and Windows-sim modes — 540/5/0 in all four. Also re-verified
  the D4a zip DID contain both earlier fixes (read back from the zip
  itself): the user's stale failures came from re-running the old
  10.D4 extraction (or __pycache__), solved by a FRESH folder.
- D4a test-only hotfix (2026-07-17, drop: Autopilot 10.D4a.zip) —
  first REAL-Windows gate found 2 test bugs of mine (541/4 green
  otherwise, both modes; Qt smoke executed +3 on Windows):
  1. D2a wav-validity guard executed the BASH fake on real Windows
     (WinError 193 — the exact reason Python fakes exist); now runs
     python-only on Windows, both variants on POSIX
  2. Qt stage-rows test asserted lowercase "trial" against the
     "Trial license — ready" status text; case-normalised
  No app code touched; structure byte-identical to 10.D4.
- D.4 PyQt6 UI skeleton (2026-07-17, drop: Autopilot 10.D4.zip):
  - ui/viewmodel.py — Qt-free brain of the window (spec File 12:
    "automated testing does not need UI"): license_summary,
    engine/module status, stage_names mirror, projects list (real
    projects table; last_render_output_path — NOT output_file_path,
    that column lives on another table), render input validation
    (RULE 7), build_render_request (exact run_script_pipeline
    contract), subscribe_pipeline + normalize_event (maps
    pipeline.* + render_progress to {stage, percent, text, level})
  - ui/app.py — thin PyQt6 shell: MainWindow (script/images/project
    pickers, title, preset, quality-gate, Render/Cancel, 17-row
    stage list ○▶✓✗, progress bar, log pane, projects list, License
    + Modules dialogs), _RenderWorker QThread, _EventBridge queued
    signal for worker->GUI event flow. HARD PyQt6 import at top:
    without PyQt6 the import fails and main.py cmd_ui prints the
    "pip install -r requirements_ui.txt" hint + EXIT_OK (seam
    contract unchanged; test_ui_command_degrades_gracefully made
    deterministic via sys.modules block so it can't hang on a
    PyQt6-installed machine; new dispatch test fakes ui.app.launch)
  - REQUIREMENTS: new optional file requirements_ui.txt (PyQt6>=6.5,<7)
  - SANDBOX LIMIT: PyQt6 wheel installs but the OS lacks
    libxkbcommon.so.0 — no Qt import possible here. Qt smoke tests
    (tests/unit/test_ui_app_qt.py: construct, invalid-input guard,
    full happy-path worker round-trip incl. bridge-fed stage rows +
    progress bar) are importorskip-guarded and skip headless; they
    EXECUTE on the user's Windows once requirements_ui is installed
  - Bugs found+fixed during D.4: stage_names() first written as
    classmethod against cls._STAGE_MODULES (module-level table —
    AttributeError, caught by new test); viewmodel SQL picked
    output_file_path (not a projects column — same drift class as
    D2a#3, fixed to last_render_output_path)
- D2a hotfix (2026-07-17, drop: Autopilot 10.D2a.zip) — Windows D.3
  smoke findings vs REAL ffmpeg (13/17 stages were green incl. real
  intro/outro renders before two warnings + one crash surfaced):
- D2a hotfix (2026-07-17, drop: Autopilot 10.D2a.zip) — Windows D.3
  smoke findings vs REAL ffmpeg (13/17 stages were green incl. real
  intro/outro renders before two warnings + one crash surfaced):
  1. CRASH (core/core_engine.py): export stage died with
     "_forward() takes from 0 to 1 positional arguments but 3 were
     given" — monitor_ffmpeg_progress calls callback(progress, fps,
     eta); POSIX fakes emit no frame= lines so tests never invoked
     the forwarder; REAL ffmpeg does, every encode. Forwarder now
     mirrors the 3-float contract (all defaulted) and publishes
     {progress, fps, eta_seconds} on pipeline.render_progress
  2. Piper per-line crash-spam (modules/tts_engine_manager.py):
     pip-installed piper was found on PATH and a sidecar-less junk
     model (test-written fake_model.onnx, DEBT-C5a) matched the
     any_onnx fallback → real piper crashed per narration line
     (~2s + full traceback each). _find_piper_model now REQUIRES the
     <model>.onnx.json sidecar (piper can't load without it); piper
     subprocess failure log reduced to the meaningful tail line
  3. Misleading DB ERROR log (core/core_engine.py): "no such column:
     file_path" per scene — _processed_image_for probed candidate
     column names by try/error; now resolves the true column once
     via PRAGMA (cached, negative cached) → silent + one fewer query
  Regression nets: the integration smoke now runs with FAKE_FRAMES=1
  so the fake ffmpeg drives the progress-forwarder end-to-end (would
  have caught bug 1); unit tests pin the 3-arg forwarder contract,
  the PRAGMA column resolution (original_file_path, no "no such
  column" ERROR in logs) and the piper sidecar rule. 3 new tests.
- D.1 core_engine orchestrator: **COMPLETE** (2026-07-16)
  - core/core_engine.py: THE documented RULE 1 seam — the only place that
    imports modules/*; registry-driven loader (modules_config.json priority
    order, disabled skipped, broken optional/required recorded, never raised)
  - Fixed v1 stage plan (17 stages): license -> parse/persist ->
    channel_profile -> voice_profiles -> keywords -> quality_gate ->
    images -> tts -> sfx -> audio_mix -> subtitles -> intro_outro ->
    timeline -> export (per-scene render -> join+audio mux) ->
    burn_subtitles -> verify -> thumbnails -> finalize
  - Stage criticality mirrors registry required flags + tts/audio_mix as
    pipeline-required; optional failures -> warnings; stages can
    self-escalate (hard quality gate)
  - License gate (active|trial pass; expired|invalid abort; module
    unavailable -> degrade to warning, never brick renders; enforcement
    switchable per run)
  - TTS stage persists narration-ABSOLUTE word_timestamps (line-relative
    seconds + cumulative line duration + 0.25s pause -> absolute ms)
  - RenderStateMachine integration w/ legal-chain walking for skipped
    stages; pipeline cancellation (honored between lines/scenes);
    reentrancy guard; CorrelationContext per run; full pipeline.* event
    stream; projects row finalized (status/render_count/output)
  - make_batch_processor(): batch_engine RULE 1 seam wired here
  - 25 unit tests (DI module fakes) + 1 INTEGRATION SMOKE with ALL REAL
    modules + fake FFmpeg: full script->MP4 pipeline against the
    sample_project fixture is GREEN (tests/integration/test_smoke_render.py;
    verify stage skipped in sandbox — fake ffprobe reports constant
    duration; real pixel/bitstream proof = Windows D.3)
  - pytest.ini: markers registered (slow/integration/e2e)
- D.2 main.py entry point: **COMPLETE** (2026-07-17)
  - main.py at project root: find_project_root (sys.frozen exe dir or
    __file__ parent), load_app_settings, build_container (real
    database/schema.sql, temp/projects/cache/logs folders), boot()
    -> ctx {container, engine: CoreEngine, license, license_data}
  - REAL product fix found by CLI smoke: _prepend_engine_dir_to_path
    puts <root>/engines/ffmpeg on os.environ PATH at boot — pydub and
    any by-name subprocess consumer resolve the bundled engines on
    every platform (they consult PATH, not HardwareService hints)
  - argparse CLI: render (--script req, --images/--project-folder/
    --title/--preset/--profile/--quality-gate/--skip-license/
    --skip-stage repeatable), render-project, check, batch, batch-add,
    modules, license [--activate KEY], ui (D.4 seam: ui.app.launch,
    ImportError -> friendly message + EXIT_OK)
  - Exit codes 0/1/2 (EXIT_OK/EXIT_FAIL/EXIT_USAGE); main(argv)->int;
    handler exception isolation (no traceback dumps; KeyboardInterrupt
    handled); batch exits 1 only when failed>0 AND stopped early
  - 21 unit tests (tests/unit/test_main.py): boot helpers, frozen path,
    real-boot first launch, every command, exit codes, isolation
  - AGENT CLI SMOKE END-TO-END GREEN (real modules, fake engines):
    all 17 stages, 16 ok + verify intentionally skipped; full
    ffprobe->audio->timeline->export->burn chain; 49 well-formed
    ffmpeg CMD lines logged (xfade offsets, AAC 48k mux, burn
    force_style); output MP4 written (12-byte FAKEMP4DATA payload
    under fakes — real pixels = D.3 on Windows)
  - Bugs found + fixed this cycle (root causes):
    1. fake-ffmpeg .wav payload bug (tests/conftest.py): precedence —
       b'\x00\x00' * 48000 * 2 // 4 raises TypeError (bytes // int,
       left-to-right); fake still exited 0 leaving 44-byte header-only
       WAVs; pydub then died downstream with "CouldntDecodeError rc 0".
       Fix: precomputed b'\x00\x00' * 24000 (48000B = 12000 stereo
       frames = 0.25s @48k) in BOTH bash and Python fakes + recurrence
       guard self-test (wave-open asserts channels/rate/frames)
    2. NUL bytes in generated Python fake: a comment added to the
       NON-RAW _FAKE_FFMPEG_PY template contained \x00 escapes which
       materialised as real null bytes in the written script
       (SyntaxError "source code cannot contain null bytes");
       comment re-escaped
    3. engines/ pollution root-caused: leftover scratch fakes from the
       CLI smoke living at ./engines made modules find "available"
       ffmpeg and defeated the without_ffmpeg graceful-failure tests
       (looked like a suite regression; was tree hygiene). Smoke
       engines now built from conftest templates on demand and REMOVED
       afterwards; stash kept outside the repo
  - Test-isolation note: audio_processor narration/crossfade tests can
    fail when run as a small cherry-picked subset (pre-existing
    order-coupling); green in every full/full-file run
  - Smoke warning noted, not a defect: subtitle "Overlap at block N:
    previous end clamped" for blocks 17-29 — every fake WAV is 0.25s so
    30 blocks compress into ~7.5s and the subtitle engine clamps
    gracefully; expected to vanish with real TTS durations (recheck D.3)
  - Sandbox note: /tmp is a 996MB tmpfs — pytest basetemp accumulation
    filled it mid-smoke (Errno 28); clear /tmp/pytest-of-user between
    heavy runs; CLI-smoke deps live in /tmp/pipuser (PYTHONPATH) —
    /tmp survives turn boundaries better than ~/.local but not reboots
- C.1 license_manager: **COMPLETE** (2026-07-16)
  - core/license_manager.py (per spec MODULE 13: core placement, CAN BE DISABLED NO)
    - generate_hwid: winreg MachineGuid + WMI cpu/mb + MAC, OEM filtering,
      sorted components, sha256 -> six 4-char uppercase groups
    - initialize_license / check_license / activate_license / get_days_remaining
      (real license_data table; trial countdown syncs days_remaining)
    - generate_tamper_hash + DB-tamper detection on activated licenses
    - detect_clock_tampering (1h tolerance; evidence preserved on detection)
    - Key decrypt: strip separators -> '~'->'-' restore -> outer b64 -> Fernet -> JSON
      (dash-safety: urlsafe-b64 payload '-' would collide with group separators)
  - license_tool/key_generator.py (standalone admin tool + CLI):
    PBKDF2HMAC-SHA256 (spec constants, 100k, ADMIN_PASSWORD) -> Fernet ->
    double-b64 per spec, dash-grouped x4, key_history.csv log
  - 26 tests (HWID determinism/filtering/binding, trial lifecycle, activation
    round-trip + rejection matrix, tamper/clock, generator + CSV history,
    byte-identical derivation guard for the RULE 1 duplication)
- C.2 quality_checker: **COMPLETE** (2026-07-16)
  - modules/quality_checker.py (MODULE 15; optional, registry priority 16)
  - All 12 spec checks: ffmpeg (-version probe, RULE 4 logged), scenes exist,
    all images (matched/missing/corrupted/low-res 1280x720 floor, Pillow
    verify), TTS engines, voice profiles (dialogue characters vs profiles
    incl. aliases), disk space (video bitrate/8*1.5 + 200MB/min temp +
    5MB/scene TTS), output folder, timeline duration (zero/short/10% drift),
    subtitle file (+ style catalog), transitions (catalog + duration),
    animations (catalog + intensity), RAM available (baseline 2048 + engine)
  - 3 REAL auto-fixes: create_output_folder (mkdir), match_existing_images
    (flip image_matched when file exists), create_voice_profiles (defaults)
  - Severity model critical/error/warning/info; readiness = no unresolved
    critical/error; per-check passed/failed/warning rollup sums to 12
  - Persists quality_check_results row; B.12-consististent size/time
    estimates; human-readable pre-render report (report_text)
  - 29 tests (green world, every severity path, auto-fix side effects,
    saved-row + report content, RAM/disk monkeypatched deterministic)
- C.3 image_processor: **COMPLETE** (2026-07-16)
  - modules/image_processor.py (MODULE 03; REQUIRED module, registry priority 4)
  - Full spec pipeline: EXIF rotate (3/6/8) -> orientation (1.2/0.8) ->
    16:9 framing (landscape cover+center-crop; portrait/square blurred-bg
    composite, GaussianBlur 20) -> RGB -> processed PNG 1920x1080 ->
    proxy 854x480 LANCZOS JPEG q70
  - process_all_images: skip-if-unchanged (size+processed present),
    'images.progress' bus events, image_assets row updates, failure ledger
  - get_image_info (incl. EXIF date/GPS summary), detect_low_resolution
    (800x600), batch_import_folder (recursive, all 7 formats incl. upper-
    case suffixes, collision-safe copies, display thumbnails, asset rows)
  - 20 tests (threshold boundaries, pixel-verified composite, EXIF crafted
    JPEGs, full process_all flow incl. reprocess-on-change and deleted-
    source failure, batch import with nesting/duplicates)
- C.4 intro_outro_engine: **COMPLETE** (2026-07-16)
  - modules/intro_outro_engine.py (optional, registry priority 12; no File 07
    spec — built from the surrounding contract: timeline_engine intro/outro
    blocks, default_channel_profile.json, channel_profiles intro_*/outro_*
    columns, documentary_genres.json intro/outro_template fields)
  - config/intro_outro_templates.json NEW: 6 templates (5 genre-referenced
    ids from File 11 + default); gradient/ornament/animation/zoom params
  - config/documentary_genres.json EXTENDED additively with File 11's
    intro_template/outro_template per genre (B.10-safe; aliases noted:
    ancient_civilizations->historical, historical->historical)
  - Settings chain: default profile json -> channel_profiles row -> genre
    fallback -> project has_intro/has_outro kill switches; clamped durations
  - Pillow cards 1920x1080 (per-row gradient, accent_bar/double_bar/
    frame_lines ornaments, wrapped text, font fallback) -> local zoompan
    (zoom_in/out/static) -> libx264 segment (fake-ffmpeg verified)
  - Custom intro/outro video path: ffprobe trim + scale/pad normalize
  - 22 tests (catalog/genre consistency guard, settings precedence, argv
    shape, pixel-verified ornaments/gradient, custom video, clamps, skips)
- C.5 thumbnail_generator: **COMPLETE** (2026-07-16, built C.5-C.8 in one
  approved batch — user instruction "Skip verification. Build C.5 C.6 C.7
  C.8 now."; verification was still achieved, see TEST STATUS)
  - modules/thumbnail_generator.py (optional, priority 14; no File 07 spec —
    contract: thumbnails table (variations 1..5, 1280x720, is_selected),
    app_settings thumbnail_count/auto_generate_thumbnails,
    channel_profiles.thumbnail_style, default_channel_profile.json)
  - config/thumbnail_styles.json NEW: 6 style cards; accent palette synced
    with intro_outro_templates.json (branding consistency guard)
  - Pure Pillow (no FFmpeg): scene-image cover-fit OR gradient fallback,
    overlay blend, radial vignette, accent_bar/double_bar/frame_lines
    ornaments (same geometry as C.4), wrapped title + channel text
  - Variation cycle: profile style first, then remaining catalog styles;
    scene sources cycle with cumulative-duration source_timestamp
  - thumbnails table persistence, exclusive select_thumbnail, delete APIs,
    auto_generate_for_project kill-switch seam, 'thumbnails.generated' event
  - 26 tests (settings chain incl. count clamp to schema 1..5, pixel-level
    gradient/ornament/overlay/vignette, cycling, persistence, selection
    exclusivity, fallbacks incl. corrupt source, event, kill switch)
- C.6 channel_profile_manager: **COMPLETE** (2026-07-16)
  - modules/channel_profile_manager.py (optional, priority 17; contract:
    channel_profiles table full column set, UNIQUE profile_name, is_default
    seed row, catalog configs for validation, projects.channel_profile_id)
  - CRUD + duplicate + set_default (exclusive) + delete (default protected;
    referencing projects reassigned to default, never left dangling)
  - RULE 8 validation: profile_name pattern/length/uniqueness, genre + all
    preset columns validated against live catalogs (skipped silently when a
    catalog fails to load — offline-safe), #RRGGBB colors normalized,
    volumes/durations/font sizes clamped with warnings, internal columns
    (id/created_at/is_default) rejected from payloads
  - create uses default_channel_profile.json defaults (field name mapping
    has_intro->intro_enabled etc. documented in code)
  - apply_profile_to_project copies 12 render-relevant fields onto the
    projects row and stores the canonical profile id; id-OR-name
    resolution mirrors C.4 (schema stores both; documented)
  - 26 tests
- C.7 voice_store_manager: **COMPLETE** (2026-07-16)
  - modules/voice_store_manager.py (optional, priority 18; contract:
    voice_store_cache + installed_voices tables, bundled
    config/voice_store_catalog.json, engines/<engine>/models layout from
    tts_engine_manager — documented RULE 1 duplication)
  - refresh_catalog: injected data / remote URL via requests / bundled
    fallback (offline-first; remote failure degrades to bundled + warning);
    catalog validation skips bad engines/missing URLs (RULE 8)
  - install_voice: injected HTTP session seam, streamed download with
    'voice_store.download_progress' events, reinstall-before-download order
    (force=True), file under engines/<engine>/models/<voice> (URL-tail
    filename), RAM + size-drift warnings, graceful failure cleanup
  - uninstall (row + owned files + emptied dir), list/get installed,
    record_voice_usage (orchestrator seam), set_voice_enabled, store stats
  - 25 tests (all network injected — zero real HTTP)
- C.8 batch_engine: **COMPLETE** (2026-07-16)
  - modules/batch_engine.py (optional, priority 15; contract: batch_queue
    table status enum + priority 1-10 + retry columns, app_settings
    batch_retry_count=3/batch_stop_on_error=false, File 12 v1 queue)
  - Queue CRUD/order, cancel/pause/resume, retry_failed, clear_finished,
    stats; processing-item mutations blocked
  - Sequential processing with dependency-INJECTED processor callable
    (RULE 1 seam: app orchestrator wires the pipeline; never imported);
    per-item isolation (exceptions become item failures), retry loop
    (attempts = 1 + max_retries, terminal failure recorded),
    started_at stamped once via COALESCE, output_file_path captured,
    stop_file operator kill-switch, stop_on_error halt, reentrancy guard
  - Events: batch.item_added/started/retrying/completed/failed +
    batch.queue_completed
  - 25 tests
B.1–B.9: Windows-verified.
B.10–B.12 first Windows checkpoint: **245 passed, 18 failed, 4 skipped** — all 18
failures were `[WinError 193] %1 is not a valid Win32 application` because the
fake ffmpeg/ffprobe test doubles were bash scripts, which Windows cannot execute.
HOTFIX B.12.1 (2026-07-16): cross-platform test doubles in `tests/conftest.py`:
- Fake file NAMES stay `ffmpeg`/`ffprobe` on every platform so
  `HardwareService.find_ffmpeg/find_ffprobe` resolution is unchanged.
- CONTENT: bash script on POSIX; sentinel-marked Python script on Windows.
- Test-only shim wraps `subprocess.run`/`Popen` and routes sentinel-marked
  fakes through `sys.executable`; all other subprocess calls untouched.
  (.bat was rejected: CreateProcess can't run batch files with shell=False,
  and cmd parsing would corrupt `&` ASS colours and `(...)` filter args.)
- Windows path is simulatable anywhere: AUTOPILOT_TEST_WINDOWS_FAKES=1.
- 10 new self-tests in tests/unit/test_fake_binary_helpers.py guard the fix.
**AWAITING WINDOWS RE-VERIFICATION of `Autopilot 8.12.1.zip`.**
No production engine code changed; engines are untouched by this hotfix.

B.10 color_grade_engine: real 14-preset config, File 07 filter chain, 20 tests.
B.11 subtitle_engine: SRT generation + force_style + animated drawtext chains, 8 styles, 19 tests.
B.12 export_engine (2026-07-16, REQUIRED module):
- render_scene_to_video (scale→zoompan→grade; LUT opacity via split/lut3d/blend;
  dust/scratch overlays via multi-input filter_complex — DEBT-B10a WIRED)
- render_title_card (Pillow card → video segment)
- join_segments_with_transitions (xfade chains, auto-grouping in tens)
- detect_hardware_acceleration (NVENC/AMF/QSV, tiny-encode verified) + encoder mapping
- monitor_ffmpeg_progress (frame=/fps= parsing, ETA, render.progress event bus)
- estimate_render_time / estimate_output_size, verify_output (ffprobe ±1s tolerance)
- Crash recovery via render_progress table (create/update/resume/finish, disk-filtered segments)
- plan_subtitle_segments → DEBT-B11a WIRED (30s windows, orchestrator applies per segment)
- Real config/export_presets.json (4 presets) + config/ffmpeg_commands.json (16 templates)
- 28 tests + 4 config-integrity tests. All configs: ZERO PLACEHOLDER_PHASE_B left.

## TEST STATUS

- Windows (user): **471 passed, 5 skipped, 0 failed** — PHASE C VERIFIED
- Linux POSIX mode (agent, Python 3.13 + audioop-lts): **541 passed, 5 skipped, 0 failed**
  - 540 (through D4b) + 1 (D.5 frozen root-resolution pinning test)
- Linux WINDOWS-SIMULATION mode: **541 passed, 5 skipped, 0 failed**
- Plus the D.5 FROZEN milestone (not in pytest): built bundle boots,
  lists 19/19 modules, renders all 17 stages (see Phase header)
- 5th skip: PyQt6 Qt-smoke file (importorskip — runs on Windows once
  `pip install -r requirements_ui.txt` has been run)
- KNOWN INTERMITTENT (sandbox-only, root-caused): /tmp is a 996MB
  tmpfs; back-to-back suites fill it (Errno 28 at collection, plus an
  occasional test_quality_checker green-world flip). Cure for agent
  runs: --basetemp=/home/user/.pytest_base (real disk). Never seen on
  Windows; if it EVER fails there with disk headroom, escalate immediately
- One transient single-test failure observed ONCE in a non-fake run this
  cycle; not reproduced in 4 subsequent full runs (both modes); kept on
  watch, no code change made on an unreproduced signal
- Skips unchanged: real Piper/Kokoro/XTTS models + reportlab (optional)
- Quality gates: black/ruff/mypy NOT runnable in this session (sandbox venv
  was partially pruned by snapshot size caps — its compiled .so files are
  gone; user directive: skip env rebuilds). Manual equivalents applied to
  all new files: 0 lines > 88 cols, AST unused-import scan clean,
  py_compile clean. Full gate re-run due at the user's Windows checkpoint.
- Windows: Phase C checkpoint DONE (471/5/0). Agent smoke PREREQUISITE for
  D.3 already green with fake FFmpeg; real-MP4 smoke on Windows is next.
- Python note: 3.10/3.11 recommended; on 3.13 `pip install audioop-lts` first

## SAFETY NETS (Arena workspace /home/user/backups/)

- pre_B9 … pre_B12, pre_WinFix, pre_C, pre_C5 zips (2026-07-16)
- GitHub `Autopilot 8.8.zip` — last pre-B9 baseline

## NEXT (Phase D, user-approved scope)

1. D.1 core_engine orchestrator: DONE (drop: Autopilot 10.D1.zip).
2. D.2 main.py entry point: DONE; superseded by D2a hotfix (this drop:
   Autopilot 10.D2a.zip); agent CLI smoke end-to-end GREEN under fake
   engines, INCLUDING FAKE_FRAMES progress-callback path.
3. D.3 SMOKE TEST on WINDOWS with REAL FFmpeg: DEFERRED by user onto
   the combined Windows gate before D.5 (D2a fixes understood+netted).
4. D.4 UI skeleton (PyQt6 main window): DONE (this drop: Autopilot
   10.D4.zip). On Windows: pip install -r requirements_ui.txt, then
   python main.py ui; the Qt smoke tests start executing too (+3).
5. D.5 Final build (Autopilot.exe).
6. DEBT-C5a: tts/voice tests write fake models under ./engines, and B.11
   subtitle runs write generated SRTs into ./subtitles (repo pollution —
   clean before every drop zip; consider redirecting like C.7 does).
7. DEBT: README.md still says "Phase: A Foundation";
   docs/FILE_INVENTORY_B8.md lists monolithic file_parser.
8. Visual QA with REAL FFmpeg renders (filter strings are unit-verified; pixels are not).
9. ENV NOTE: sandbox auto-prunes ~/.local + venv compiled files between
   turns; deps top-up is one pip command (documented in TEST STATUS).
10. KNOWN LINT NIT (pre-existing, shipped D1/D2/D2a): one 91-char line at
    tests/unit/test_tts_engine_manager.py:299 (plain string arg; black
    tolerated it since Phase B — black does not split long strings).
    Not churned in the D2a hotfix; fix at the next cosmetic pass.

## PHASE B HISTORY (closed)

- B.12 Windows checkpoint: 245 passed / 18 failed / 4 skipped (WinError 193
  bash fakes) → B.12.1 hotfix → Windows 271 passed / 1 failed / 5 skipped
  → B.12.1a byte fix (write_bytes in python fake) → Phase B VERIFIED.

## UI.4 — SPEC FILE 04 FIVE-FEATURE POLISH: SHIPPED 2026-07-18 (Autopilot 17.FINAL.zip)

User directive (verbatim intent): build the 5 spec features, test only
at the end, move fast, ship 1.0. Delivered:

1. IMPORT PANEL — three separate drop zones, formats printed on each:
   Script "TXT · JSON · CSV · DOCX · PDF" (JSON support is VISIBLE),
   Images "JPG · JPEG · PNG", Audio "MP3 · WAV". IMPORT_KINDS now
   matches the spec list exactly (webm/flac/md etc. classify "other"
   and are flagged unsupported honestly). File dialog filter rebuilt
   around the same list. ui/viewmodel.py IMPORT_ZONES + import_zones();
   ui/panels/import_panel.py DropZone(kind/formats/hint).
2. VISUAL TIMELINE — scene CARDS (not a table): real thumbnail from
   scenes.proxy_image_path/image_file_path (painted 16:9 placeholder
   when missing), duration, animation type, status, start timecode,
   first dialogue line. timeline_model enriched (thumb_path, start,
   start_text, chapter); card text is pure view-model
   (scene_card_lines) pinned headless. ui/panels/timeline_panel.py
   rewritten (QScrollArea + SceneCard; QTreeWidget removed).
3. FULL MENU BAR — 7 menus in spec order: File/Edit/View/Project/
   Render/Tools/Help. New actions: refresh_projects (F5), Settings now
   also under Edit, All Projects, Open Project Folder, Project
   Timeline, Pre-Render Report (Ctrl+Shift+R), Plugins, Engines, Open
   Logs Folder. One ACTION_DEFS table still drives menus AND toolbar
   (toolbar 6 buttons unchanged, order pinned). Shared QActions across
   menus where an item lives in two places (export_video, etc.).
4. PRE-RENDER REPORT dialog (NEW) — pre_render_report_model(): script
   exists/format/size/word count, images count, folder writable +
   free space, FFmpeg/Piper presence, license, Drive; ✓/⚠/✗/ℹ rows,
   duration estimate (~150 wpm) + rough scene count; Start Render
   enabled only when errors == 0. Render menu item, Ctrl+Shift+R, and
   a "📋 Report" button on the render form.
5. RENDER COMPLETE dialog — thumbnail preview (thumbnails table,
   is_selected first, falling back to a scene image), duration from
   render_history, YouTube chapter block (first chapter forced to
   0:00) with "⧉ Copy chapters", Play video / Open folder / Upload to
   Drive preserved. Model is additive-only (old keys untouched).

Fixed while wiring: the Qt studio-dispatch smoke test dispatched
new_project, which opens a MODAL dialog — it would have hung the
Windows pytest run (these Qt tests run there only). Rewritten around
non-modal actions; also added an unknown-action fallback pin.

Gates: py_compile OK, no over-length lines (awk byte-count blip on
the Devanagari test line is 74 codepoints), unused-import scan clean,
no trailing whitespace.
Suite: 622 passed / 5 skipped / 0 failed BOTH MODES (living repo AND
extracted zip). Windows expectation: 637 / 4 / 0 (15 Qt tests execute
there — one NEW: pre-render dialog construct).
Backup: backups/Autopilot_Backup_SPEC_FINAL_2026-07-18.zip
Drop: Autopilot 17.FINAL.zip
REMINDER: push to https://github.com/sameershinde6293/Autopilot

## UI.5 — ui_specification.txt RECONCILIATION: SHIPPED 2026-07-18 (Autopilot 18.SPEC.zip)

User pasted the REAL spec deltas (Sections 5,8-17 + workflow_spec);
the 200+ enterprise checklist was rejected as never-in-spec. Built
EXACTLY the listed items, in the mandated order:

1. PREVIEW TABS (§8): QTabWidget Preview/Storyboard/Scene Details;
   scene info bar under the player (scene_at_position vs DB timings);
   storyboard thumbnail grid (click -> details tab); full details rows.
2. VISUAL TIMELINE (§9): real thumbnails kept; MarkerStrip (chapter
   lines, % of total, chapter_title or every-scene-start fallback);
   WaveformStrip (PCM .wav peaks via stdlib wave, honest notes);
   right-click scene menu (copy/paste/delete); drag-to-reorder via
   per-card press/release math -> vm.reorder_scene renumbers the DB.
3. FULL MENU (§5, verbatim): File New/Open/Save/Import ZIP/Export/
   Backup/Quit; Edit Undo(Ctrl+Z)/Redo(Ctrl+Y)/Select All/Copy/Paste/
   Delete Scene (snapshot-based undo stack, 20 deep); View Dark/Light/
   AMOLED/High Contrast + toolbar/statusbar/progress toggles; Project
   Settings/Channel Profile Manager/Quality Check/Pre-Render Report;
   Render Start(F9)/Quick Preview(F5)/Cancel/Pause(honest-disabled)/
   Resume/Batch/Settings; Tools Voice Store/Voice Clone/Engine
   Manager/Key Generator + Modules/Plugins/Logs; Help User Guide/
   Shortcuts/About.
4. DIALOGS (§15): Voice Clone (queues cloned_voices rows, honest
   "awaiting XTTS engine"), Engine Install/Manager (live-detect
   FFmpeg/FFprobe/Piper), Admin Key Generator (honest boundary: HWID
   helper, no key minting in user builds). Recovery dialog existed.
5. GRADE PAGE (§10): tabs Grade/Animation/Transition/Export; sliders
   Brightness/Contrast/Saturation/Vignette/Film Grain + LUT combo +
   opacity -> scenes.color_grade_override JSON; presets -> projects.
   color_grade_preset; Apply to All Scenes. Real columns throughout.
6. AUDIO PAGE (§11): narration/music/sfx volumes + music path ->
   projects narration_volume/music_volume/sfx_volume/music_file_path;
   ducking on/off (app_settings ducking_enabled) + depth/ceiling/
   attack/release saved + master_volume; preview via preview player.
7. VOICE STORE PAGE (§13): search + gender/language filters; voice
   cards (engine/lang/quality stars/size/installed); Install/Remove
   via voice_store_manager seam (honest unavailable without engine);
   DB-cache listing fallback.
8. BATCH PAGE (§14): batch_queue table flow (priority, status icons,
   add-current, remove queued-only, priority nudge); Start runs the
   queue SEQUENTIALLY through the same engine + worker; Stop cancels.
9. PROGRESS PANEL (§12): collapsible bottom strip on ALL pages —
   stage chips with marks/colors, % + progress bar, live log, Pause
   (honest-disabled)/Cancel; fed by the same normalized pipeline
   records as the render page monitor.
10. NOTIFICATIONS (§17): top-right slide-in toasts, info/success/
    warning/error icons, 4s auto-dismiss, restack on dismiss.
11. WORKFLOWS (workflow_spec): first-run wizard when engines missing
    (persisted once dismissed), Channel Profile Manager (duplicate/
    delete/set-default via config), autosave rotation
    backups/autosave_1..3.zip on a QTimer using the REAL
    auto_save_interval_seconds (300) setting.

FIXED WHILE BUILDING (root-caused): undo stack name-mangling bug
(property + "__undo" string is never mangled -> stack silently
empty); batch_queue id second-resolution collisions (microseconds);
_database_file now reads db.db_path (config json value is a RELATIVE
path, and app_config overrides aren't exposed via config.get);
audio_tracks has created_at but no updated_at; production
ConfigService.set writes real files -> tests use instance-level
recorders (no repo pollution). SANDBOX PRUNE reverted the 4 UI test
files to 16.UI3 mid-turn — restored from 17.FINAL zip, then
re-pinned for the new spec (caught by suite diff, not by luck).

Gates: py_compile OK on all UI files; <=88 cols; AST unused-import
scan clean (5 fixed); no trailing whitespace.
Suite: 641 passed / 5 skipped / 0 failed BOTH MODES.
Windows expectation: 656 / 4 / 0 (15 Qt tests execute there).
Backup: backups/Autopilot_Backup_SPEC_FULL_2026-07-18.zip
Drop: Autopilot 18.SPEC.zip
REMINDER: push to https://github.com/sameershinde6293/Autopilot

## UI.6 — SHIP 1.0 (ui_specification.txt 33-item closure) — 2026-07-18

User pasted the REAL ui_specification.txt audit (33 items: 10
CRITICAL / 8 HIGH / 7 MEDIUM / 8 VISUAL). Built everything still
missing on top of 18.SPEC:

* CRITICAL new: Splash with badge logo + REAL loading bar + version;
  License Screen dialog with HWID display + Copy + activation
  (shows at boot ONLY when no active/trial license — never nags);
  3-panel layout (left nav | center pages | right Inspector card
  mirroring timeline scene selection); Import Panel now 5 drop zones
  (Script / Images / Music / Voice-over / Video clips; new "video"
  import kind classifies mp4/mov/mkv/webm and stages imports/videos).
* HIGH new: toolbar channel-profile dropdown (synced to the render
  form combo) + toolbar license status label; icons on EVERY menu and
  toolbar action (ACTION_ICONS, unicode — zero binary assets).
* MEDIUM new: fullscreen toggle (F11, View menu), window memory
  (geometry/state base64 via config, restore at boot/save on close),
  tooltips on nav/pages/toolbar extras.
* VISUAL new: Montserrat font family (QSS + QFont, honest platform
  fallback), QFrame#card + cardTitle/h2 hierarchy, gradient progress
  chunk, QListWidget/tab/slider-handle hover effects, dock title
  styling.

Bugs/gotchas this round: UiViewModel ctx key for the license manager
is "license" (not "license_manager") — one test pin fixed. Sandbox
prune CONFIRMED again: all 5 UI test files were reverted to 16.UI3;
restored from 18.SPEC zip, then re-pinned for the new spec (zones
kinds list, webm classifier, toolbar labels with icons + widget
actions). Sources verified untouched vs 18.SPEC (additions-only
diffs) before editing continued.

Gates: py_compile OK; <=88 cols; AST unused-import scan clean; no
trailing whitespace. Suite: 651 passed / 5 skipped / 0 failed BOTH
MODES (8 new headless ship_vm tests; +5 Qt tests run on Windows).
Windows expectation: 671 / 4 / 0 (20 Qt tests execute there).
Drop: Autopilot 1.0.FINAL.zip
REMINDER: push to https://github.com/sameershinde6293/Autopilot

## UI.7 — Deep-UI-analysis fixes → 1.0.1 — 2026-07-18

User ran the app and pasted a 12-item deep-dive list. All 12 done:

* C1 dark theme: root cause = Windows native style ignores QSS on
  many controls -> app.setStyle("Fusion") BEFORE apply_theme, palette
  retuned to spec #1A1A2E family, QSS widened (QTextEdit, spinboxes,
  QScrollArea, QGroupBox, QMessageBox labels).
* C2 Voice Store: engine-install wizard strip (status + Engine
  Install + Setup Wizard buttons opening real dialogs).
* C3 + M9 Inspector: app stats when nothing selected (license,
  projects, modules, plugins, FFmpeg, Piper) + quick actions
  (New/Render always; Copy/Delete with a scene selection).
* C4 toolbar: Pause (honest disabled+reason), Batch, User Guide
  added — model-driven, so menus/pins stay in sync.
* C5 status bar: Project / FFmpeg / RAM·CPU fields refreshed on a 5s
  QTimer; vm.system_status_model is stdlib-first (psutil optional,
  /proc fallback, em-dash honesty).
* H6-H8: rich empty states (timeline icon+CTA, batch, projects,
  preview, import); tooltips on EVERY button (global pass + hand
  tips); menus now display Ctrl+C/V/Del, Ctrl+Shift+B/V, F1, F11
  (defaults + config json extended — config wins, so both changed).
* M10/L11/L12: toasts on save + theme switch (pre-existing toast
  system wired wider); hover/card polish kept; Setup Wizard
  re-runnable from Tools menu.

Sandbox prune CONFIRMED twice more this turn (test files reverted to
pre-1.0.FINAL; ~/.local deps wiped mid-turn) — restored from the
1.0.FINAL zip + pip ritual. Gates: py_compile / <=88 / AST unused /
no trailing WS all OK. Suite: 658 passed / 5 skipped / 0 failed
BOTH MODES. Windows expectation: 681 / 4 / 0 (23 Qt tests).
Drop: Autopilot 1.0.1.zip
REMINDER: push to https://github.com/sameershinde6293/Autopilot

## UI.8 — 53-item closure: exports (35-39) + fades (21) → 1.1.0 — 2026-07-18

User's mega-list audited against 1.0.1: items 1-34 and 40-53 already
shipped. Genuinely new code this round:

* 21 Audio fade in/out: fade_in_seconds/fade_out_seconds spinboxes on
  the Audio page, persisted to app config keys (same path as ducking)
  — applied by the mixer at next render (honest note on the panel).
* 35-39 File>Export submenu (real QMenu submenu, model-driven):
  - Audio Only: TTS via engine.module("tts_engine_manager") with
    guarded signature tries; without the seam -> honest message
    pointing at the full-render narration WAV.
  - Audio Mix: FFmpeg amix (narration 1.0 / music 0.35 / sfx 0.8),
    wav pcm_s16le or mp3 libmp3lame; injectable runner for tests.
  - Burn Subtitles: FFmpeg subtitles filter + -c:a copy; Windows
    path escaping for the filter.
  - Thumbnails Only: vm builds job list from the timeline model;
    shell scales via QPixmap on the GUI thread (320x180 JPG q90).
  - Storyboard PDF: stdlib-only writer (Helvetica text, JPEG thumbs
    via DCTDecode, 5 scenes/page, xref/trailer correct) — zero deps.
* ExportJobDialog: one parameterized form per kind; exports run on
  _RenderWorker with progress panel + toast; every FFmpeg command is
  logged to the render log (RULE 4).

Sandbox reverts CONFIRMED twice more (round-2 wiped mid-turn; tree
re-based from the 1.0.1 zip and round-3 replayed; deps wiped again).
Gates: py_compile / <=88 / unused-import / trailing WS OK. One honest
fix during gates: run_ffmpeg's early return tuple paren placement.
Suite: 672 passed / 5 skipped / 0 failed BOTH MODES.
Windows expectation: 695 / 4 / 0 (23 Qt tests).
Drop: Autopilot 1.1.0.zip
REMINDER: push to https://github.com/sameershinde6293/Autopilot

## 2026-07-19 — UI.9 / v3.0 Studio (Autopilot 3.0.zip)

Voice Controls, Transitions, Scene Controls, Subtitle Designer and
Export Settings pages (nav 8 → 13). Dockable Inspector (Priority-3
#15), 10 workspaces with toolbar selector (#16), voice preview via
the TTS seam (#17), stdlib waveform peaks on the Audio page (#18),
mixer mute switches, subtitle ASS force_style riding every burn,
export-profile codec/CRF/preset consumed by Burn Subtitles.
Suite: 692/5/0 sandbox both modes; Windows expectation 715/4/0.

## 2026-07-19 — UI.10 / Review fixes (Autopilot 3.0.1.zip)

Seven expert-review fixes: Inspector elision + tooltip paths,
duplicate Inspector header removed, compact stage strip (no more
clipped 18-chip row), Grade page hosts grading only, subtitle
colour buttons are plain swatches, engine indicators with tooltip
paths. Includes the carried 3.0 PEP-701 (Python 3.10) footer fix.
Suite: 693/5/0 sandbox both modes; Windows expectation 716/4/0.

## 2026-07-19 — UI.11 / Review round-2 (Autopilot 3.0.2.zip)

Master volume now round-trips (default in model + config overlay +
reload_settings). Transitions panel: currentItemChanged ->
_load_selected (mirrors scene panel). Voice Controls: reverb type
and breathing toggle precede their amount sliders; value labels
fixed 48px. Suite: 694/5/0 sandbox both modes; Win exp. 717/4/0.

## 2026-07-19 — UI.12 / Review round-3 (Autopilot 3.0.3.zip)

Key-generator renamed to "My License / Machine ID" (visible text
only). Batch queue + Transitions keep selection across rebuilds.
Scene Details labels 6 (matches model rows minus title). Voice Store
language filter is catalog-driven (voice_languages helper). Voice
Clone requires consent checkbox before "Queue clone".
Suite: 696/5/0 sandbox both modes; Win expectation 719/4/0.

## UI.13 — Expert-review round 4 (3.0.4) — 2026-07-20
1. Pre-render match report: `pre_render_match_report()` runs the fuzzy
   match (engine module when installed, thefuzz/difflib local fallback)
   and the dialog gains a coloured Scene | Image | Status | Confidence
   section (green exact / yellow fuzzy / red unmatched) + summary line.
2. Render Complete: `render_warnings_list()` + expandable
   "▸ Show N warning(s)" section listing each ⚠ warning inline.
3. Revert buttons (↺ Revert / revertBtn) on Voice Controls, Audio,
   Export Settings, Subtitle Style and Grade panels — confirm dialog,
   then reload last saved values (Grade resets to defaults; documented).
4. Waveform: click-to-seek `seekRequested(seconds)` (+`seek_requested`
   alias), playhead line (#E94560, `set_playhead`), bottom time axis,
   near-clipping legend; `audio_file_duration()` feeds the axis.
Suite: 700 passed / 5 skipped / 0 failed (sandbox, both modes,
extraction-verified). Windows expectation: 700 headless, 727 full
(+27 Qt tests — PyQt6 cannot import here; 1 skip entry in sandbox).

## UI.14 — CRITICAL HOTFIX (3.0.5) — 2026-07-20
Startup crash: `'MainWindow' object has no attribute 'inspector'`.
Root cause: `_build_inspector()` calls `_set_inspector_lines()` which
read `self.inspector.width()` before `self.inspector` was assigned —
introduced by the 3.0.1 elide fix; present in every 3.0.x build.
Invisible in the sandbox because PyQt6 cannot import there (all Qt
tests skip; headless suite was 100% green while Windows startup died).
Fix: getattr guard with 264px fallback in `_set_inspector_lines`
(app.py). New Qt startup pin. ALSO: living tree had been pruned to a
pre-3.0 app.py + missing panels/tests; restored entire tree from the
verified 3.0.4 zip (zips remain source of truth). Version shipped as
3.0.5, not 3.0.4.1: the installer cross-check pins 3-part semver.
Suite: 700/5/0 both modes, extraction-verified from the zip.
Windows expectation: 700 headless · 728 full (+28 Qt).

## UI.15 — Layout readability round 5 (3.0.6) — 2026-07-20
Screenshot-reported truncation: Inspector middle-elide cut the app
name ("Autopilot 3.0.5 ...cumentary") and the scene hint; panels too
narrow/cramped. Fixes: nav rail 168→180px, Inspector card 230→280px
min (340 max), center pages >=600px min; `_set_inspector_lines`
drops elide entirely — word-wrap (Qt breaks long paths anywhere) +
full-text tooltip; hint merged to one full sentence; EVERY page's
QFormLayout gets 12/10px spacing, QLineEdit >=340px, QComboBox
>=220px (Settings included, not Settings-only); QSS: card/nav-item/
button/field padding 8px+. Note: base font was already 13px global —
the screenshots' cramped look came from widths/elide, now fixed.
Suite: 700/5/0 both modes, extraction-verified. Windows: 700
headless · 729 full (+29 Qt incl. new readability pin).

## UI.16 — Slider readability round 6 (3.0.7) — 2026-07-20
Export Settings CRF slider → QSpinBox 0-51 (typed, precise; tooltip
keeps the 14-35 guidance); vm whitelist clamp widened 14-35 → 0-51
(codecs' true domain; 14-35 values still valid — no breakage). All
remaining sliders on Audio (volumes, ducking, fades, master), Grade
(7 + LUT opacity), Voice Controls (5) and Subtitle Style (opacity)
keep their right-side value labels, now objectName "sliderValue" with
QSS: bold 700, 13px, min-width 50px, 6px left pad. Health: dead
QSlider/Qt imports removed from export panel. Suite 700/5/0 both
modes, extraction-verified. Windows: 700 headless · 730 full (+30 Qt).

## UI.17 — Voice Controls readability (3.0.8) — 2026-07-20
Pause QSpinBoxes (comma/sentence/paragraph/chapter) min-width 100px
(ms suffix already shown); preset Apply/Save-as/Delete buttons
min 80x30; reverb combo min-width 200; pronunciation QLineEdit
min-width 300. Engine/emotion/voice combos (>=220) and sample/pron
fields (>=340) were already covered by the 3.0.6 global pass. Suite
700/5/0 both modes, extraction-verified. Windows: 700 · 730 (+30 Qt).

## UI.18 — Three-panel readability sweep (3.0.9) — 2026-07-20
audio_panel: project combo 200, music path edit 300, fade
QDoubleSpinBoxes 100, all slider value labels 60px + inline bold
13px. subtitle_style: font/weight/position/animation combos 200,
size/outline/shadow/margin spins 100, opacity label 60px bold, color
buttons now swatch + bold hex (auto black/white contrast, 92x24).
voice_controls: engine/voice/emotion combos 250, preset combo 200,
sample edit 300, slider labels 60px bold (pause spins already 100px
from 3.0.8). Suite 700/5/0 both modes, extraction-verified.
Windows: 700 headless · 730 full (+30 Qt).

## ENGINE.1 — CRITICAL render blocker `-i .` (3.1.0) — 2026-07-21
User log: FFmpeg invoked `-y -loop 1 -i . -t 6.742 ...` → "Error
opening input: Permission denied", export stage failed. Root cause
chain: (1) images stage stored image_file_path=NULL silently when
the file wasn't found; (2) _scene_for_render passed "" downstream;
(3) export_engine ran Path("")=="." and exists() is True for a
directory, so the guard passed and "." became the -i input. Fixes:
export_engine guard (empty path → early descriptive error naming
the scene; is_file() rejects dirs; logs resolved path; also accepts
image_file_path key), images stage emits loud per-scene warnings,
_scene_for_render re-resolves from image_filename + --images folder
(self-heal for moved projects). 3 engine pins added. Suite 703/5/0
both modes, extraction-verified. Windows: 703 · 733 (+30 Qt).

## PHASE 6 — Natural Pauses & Human Pacing — 2026-07-27

Goal: make narration sound significantly more human WITHOUT regressing
anything from Phases 1-5 (audio stability, voice effects, click removal,
transition refinement, hard-intro removal, paragraph-based TTS).

Root problem: every boundary between two narration lines received the
exact same flat gap (0.25s) — `core_engine._PAUSE_BETWEEN_LINES`,
mirrored as `build_narration_track(pause_seconds=0.25)` and again as a
hardcoded `pause_between_lines = 0.25` in `timeline_engine`. A perfectly
constant interval is the loudest "machine reading" cue in otherwise good
narration, and the value was duplicated in three places.

NEW: `core/narration_pacing.py` — ONE source of truth for inter-line
silence. Lives in `core/` (like `time_helper`/`errors`) so all four
consumers can import it without breaking RULE A (modules never import
each other). Pure, stdlib-only, no I/O, no numpy/pydub.

What it decides, in order (each step numbered in `pause_after_line`):
 1. Authored-tag de-stacking — a line ending in `[PAUSE:LONG]` /
    `[SILENCE=2s]` already has that silence baked into its clip by
    `insert_pauses_into_audio`; only a clean 0.08s join is added, never
    a second full gap stacked on top.
 2. Author intent from `pause_after` — `none` runs straight on (0.0),
    `micro`/`medium`/`long`/`dramatic` are honoured. The schema DEFAULT
    `short` deliberately falls through to punctuation, otherwise every
    unannotated line in every existing script would collapse back to
    one flat value — exactly the robotic rhythm being removed.
 3. Punctuation context — paragraph .62 / ellipsis .52 / question .40 /
    exclamation .36 / sentence .34 / colon .30 / semicolon .28 /
    dash .28 / comma .20 / no-terminal-punctuation .12. Looks through
    trailing quotes and brackets (`he whispered."` is a sentence end)
    and strips bracket tags first.
 4. Emotion-aware pacing (28 emotions + the documentary aliases):
    urgent .80 → solemn 1.22. Deliberately gentler than
    `tts_presets.PAUSE_EMOTION_MULTIPLIERS`, which scales authored
    IN-LINE beats; between lines the same ±30% would read as dead air.
 5. Speaker change ×1.22, scene change ×1.35 — real turn-taking and
    structural beats.
 6. Paragraph-batch de-stacking ×0.55 — PHASE 5 interaction: inside a
    batch the engine already voiced a prosodic sentence pause that
    `split_paragraph_audio` keeps in the clip tail.
 7. Excessive-silence guard — a DERIVED gap may not exceed 90% of the
    speech it follows (a long hole after a three-word line reads as a
    dropout). Authored beats are exempt.
 8. Breathing-aware — after 14s of unbroken speech the next boundary
    widens to at least 0.34s so the narrator has somewhere to breathe.
 9. Deterministic humanising jitter ±12%.
10. Anti-robotic-rhythm nudge (never repeat the previous interval),
    breath floor re-asserted, then clamped to 0.05-2.50s.

DETERMINISTIC BY DESIGN: jitter comes from a CRC32 of the line text +
its index, NOT `random` — Python's `hash()` is salted per process and
`random` is unrepeatable. The same plan is derived independently by the
orchestrator (word-timestamp offsets) and by `timeline_engine` (scene
durations); anything non-repeatable there would silently desynchronise
images/subtitles from the voice.

SYNCHRONISATION (the critical invariant, verified end-to-end): ONE plan
flows through the whole pipeline.
 * `_stage_tts` plans every gap once from the REAL measured durations
   (after synthesis, so paragraph-batched and failed lines are known),
   uses it for `_insert_word_timestamps` offsets, and publishes
   `ctx["line_pause_seconds"]` (per join) + `ctx["line_pause_by_id"]`.
 * `_stage_audio_mix` passes it as `settings["pause_plan"]` →
   `generate_final_mix` → `build_narration_track(pause_plan=...)`, which
   inserts exactly those gaps and echoes back `pause_plan_used`.
 * `_stage_timeline` hands the id-keyed map to `build_timeline(
   line_pauses=...)` → `calculate_scene_durations`, which resolves in
   three tiers: authoritative map → deterministic recompute → flat
   legacy gap. Verified: scene durations sum exactly to
   `narration_duration`.

BACKWARD COMPATIBILITY: `natural_pauses_enabled=false` in
app_settings.json returns a flat 0.25s plan everywhere — byte-identical
Phase 1-5 behavior. Every integration point also falls back to the flat
constant on ANY failure. `build_narration_track` without `pause_plan` is
unchanged; short/over-long/NaN/garbage plans are normalised to exactly
N-1 non-negative gaps. `build_timeline`/`calculate_scene_durations` gain
only optional trailing arguments, and the orchestrator passes the new
kwarg only when `_accepts_kwarg` confirms the callee accepts it — DI
test doubles with the old signature keep working untouched.

CONFIGURABLE (config/app_settings.json, 6 keys): natural_pauses_enabled,
narration_pause_base_seconds, narration_pause_min_seconds,
narration_pause_max_seconds, narration_pause_jitter,
narration_breath_interval_seconds. Finer-grained tables (punctuation,
emotion, multipliers) live in `narration_pacing.py` next to each other,
same convention as `tts_presets.py`.

COST: planning is O(lines) — 26ms / 41KB for a 5000-line script.
`build_narration_track` caches one silence buffer per distinct gap
length (33 buffers for 59 joins in a real assembly), so allocation stays
flat instead of growing per join.

ALSO: `TTSEngineManager.plan_narration_pauses()` seam for callers that
only hold the TTS module; `narration_pacing` added to the PyInstaller
spec's `_CORE_MODULES` so the frozen exe embeds it.

Files: core/narration_pacing.py (new), core/core_engine.py,
modules/audio_processor.py, modules/timeline_engine.py,
modules/tts_engine_manager.py, config/app_settings.json,
build/autopilot.spec, build_manifest.json.

Gates: py_compile / full-package import sweep clean; no added line >88
cols; no trailing whitespace; no unused imports. Suite: 734 passed / 35
failed / 6 skipped / 14 errors — BIT-IDENTICAL to the pre-change
baseline captured from unmodified main (every failure pre-existing:
optional deps absent in this sandbox). ZERO regressions. Functional QA
and QA renders are performed by the user.

## PHASE 7 — Emotion & Prosody Enhancement — 2026-07-27

NEW: `core/narration_prosody.py`, a pure deterministic planner for
context-smoothed emotion rate, phrase groups, subtle pitch contours, and
important-word stress. It uses authored emotion when available and a complete
neutral fallback when it is not. Phrase boundaries come from punctuation,
discourse connectors, and bounded word counts; emphasis prioritizes contrast,
numbers, names, acronyms, emotional cues, novelty, and sentence focus without
over-stressing every content word.

`core_engine._stage_tts` now plans the ordered narration as one context before
parallel generation, carries each row's authored emotion into its voice profile,
and preserves line-local plans through Phase 5 paragraph batching. The TTS seam
also exposes `plan_narration_prosody()` and safely creates a standalone plan for
existing direct `generate_audio()` callers.

The TTS DSP applies smooth raised-cosine word-energy envelopes and very subtle
phrase-local pitch movement. Every phrase transform pins both endpoints, so its
duration and the full clip duration are unchanged; word timestamps are
inverse-mapped through the exact same transform. Existing voice effects,
click repair, edge fades, paragraph splitting, Phase 6 pause planning, and
breathing run in their established order.

Synchronization hardening included: global pitch shift now reads the real WAV
sample rate and compensates `asetrate` with `atempo` to preserve duration;
legacy randomized inline pauses persist the exact duration inserted and reuse
it for timestamp offsets instead of drawing a second random value.

CONFIG: `prosody_enhancement_enabled`, `prosody_emphasis_strength`,
`prosody_intonation_strength`, `prosody_transition_smoothing`,
`prosody_phrase_max_words`, and `prosody_max_emphasized_words`. Disabling the
master switch bypasses new rate/contour/stress planning and retains the existing
preset delivery path. Planning and DSP are both best-effort and fall back
without failing a render.

## PHASE 8 — Rendering & Export Optimization — 2026-07-27

Goal: cut render/export wall time and memory without changing a single
audible or visible characteristic produced by Phases 1-7. Nothing about
audio stability, voice effects, click removal, transition refinement,
hard-intro removal, paragraph narration, the pause planner, prosody,
subtitle sync, timestamps, timeline generation or scene timing was
touched — only how much work the machine does to produce them. Every
change was A/B verified against unmodified main on the same inputs.

NARRATION ASSEMBLY (the single biggest win). `build_narration_track`
grew one AudioSegment by `combined = combined + seg` per line, so pydub
re-copied the ENTIRE assembled track at every join — quadratic in line
count. It now keeps a list of finalized chunks plus a small working tail
and flushes anything older than the reachable window (crossfade length +
the 5ms zero-crossing search), then streams those chunks straight into
the WAV instead of materializing one giant segment to export. At 48kHz a
millisecond is exactly 48 frames, so every cut lands on an exact frame
boundary and the bytes are unchanged. Measured on 300 lines / 17 min of
narration: 149.1s -> 4.35s (34x), output SHA-256 IDENTICAL.

DUCKING / SILENCE DSP. `_speech_mask`, `_fill_short_silences`,
`_smooth_envelope`, `detect_silence_regions` and `_bool_runs_to_regions`
each ran a Python loop over 48,000 samples per second of audio. All are
vectorized: windowed RMS via a strided view (with an explicit-loop
fallback for any NumPy without `sliding_window_view`), run detection via
one diff, mask painting via a prefix sum, and the one-pole smoother via
its exact closed form per constant run — a ducking envelope is
piecewise constant, so `y[k] = x + (y0-x)(1-alpha)^k` is the same
recursion, evaluated in bulk. Agreement with the old loop is ~1e-13,
orders of magnitude below the 16-bit quantization step: the rendered PCM
is bit-identical (asserted directly, not inferred). Measured on a
2-minute narration + music bed: ducking 4.44s -> 0.78s (5.7x), mix
0.40s -> 0.35s, silence scan 0.57s -> 0.19s (3x); ducked and mixed WAVs
SHA-256 IDENTICAL.

DECODED-AUDIO MEMO. The mix pipeline decoded the same WAVs repeatedly —
each stage validates its input AND its output through `_validate_audio_
file` (a full decode), the next stage decodes the identical bytes again,
and the final mix is read four more times for the click pass plus the
peak/LUFS measurements. `_read_audio` now memoizes per file identity
(resolved path + size + mtime_ns), hands out read-only buffers (exactly
what the `np.frombuffer` path always returned) and evicts LRU past a
256MB budget, never storing an entry over 128MB. Every path that
rewrites a file — `_write_audio`, pydub export, ffmpeg loudnorm, the
limited-mix copy — drops its entry, and the whole memo is released when
`generate_final_mix` returns, so nothing is held for the rest of a
render.

CLICK REPAIR. `_repair_click_spikes` copied the entire buffer up front
even though it almost always finds nothing. Detection now runs on a
read-only view and the copy happens only when there is something to
repair. Same detector, same repair, same counts.

EXPORT. Group joins (>10 segments) now render concurrently (bounded at
3, like the existing scene-render pool) instead of strictly serially,
and their intermediate `.groupN.mp4` files — gigabytes on a long
project — are deleted once the boundary join has consumed them. A
single finished group is moved into place with `os.replace` instead of
`read_bytes()`/`write_bytes()` (was a full read + full write + the whole
file in RAM). `_mux_audio` no longer starts an ffprobe for the audio
duration it only uses when the video's own duration is unprobeable. The
lut3d-stripping regex is memoized per grade string. ffmpeg is spawned
with stdin closed and line-buffered stderr. Prebaked grade stills use
PNG compression level 1 — lossless either way, so identical pixels reach
the encoder.

ORCHESTRATOR. `_stage_export` resolved every scene twice (once for the
pre-flight missing-image validation, once when rendering it) — each
resolution costing a DB lookup and several filesystem probes; the
resolved dicts are now reused. `_timeline_scene_for` did a linear scan
per scene (quadratic overall) and is now an indexed lookup cached ON THE
ORCHESTRATOR, deliberately NOT inside the timeline dict, which is
serialized into `timeline_json` — an extra key there would change what
existing projects store. The index is rebuilt whenever a different
scenes list is seen.

IMAGES. `generate_proxy` re-opened and re-decoded the full-size PNG the
caller had just written; it now accepts the in-memory frame (optional
argument — omitting it keeps the original read-from-disk path exactly).
Processed PNGs are written at compression level 1: lossless, identical
pixels, without spending most of the per-image time deflating a frame
ffmpeg decodes moments later.

CACHE / FFMPEG LOOKUP. A cache HIT used to rewrite the entire index JSON
just to stamp `last_access`; that is now in-memory and flushed with the
next structural change or after 30s (it only orders LRU eviction).
`put_file` copies through the filesystem instead of loading every byte
into a Python bytes object. `_find_ffmpeg` in both audio_processor and
tts_engine_manager caches its resolution — every narration line was
re-scanning PATH — revalidated with `is_file()` each call, so a
removed/moved binary still falls back exactly as before.

BACKWARD COMPATIBILITY: no public API changed (the one new parameter,
`generate_proxy(source_image=...)`, is optional and defaults to the old
behavior); no config keys added or changed; no schema or file-format
change; no new module, so build/autopilot.spec is untouched. Every
optimization degrades gracefully — the strided-window view, the
closed-form smoother, the memo, the parallel group joins and the ffmpeg
path cache all fall back to the previous code path on any failure.

Gates: py_compile clean; full-package import sweep clean; all config
JSON valid; PyInstaller spec parses and its hiddenimports resolve; no
added line >88 cols; no trailing whitespace; no dead code, debug output
or TODOs. Suite: 709 passed / 35 failed / 6 skipped / 14 errors —
BIT-IDENTICAL failure set to the baseline captured from unmodified main
in the same sandbox (every failure pre-existing: optional deps absent).
ZERO regressions. Functional QA and QA renders are performed by the user.

Files: modules/audio_processor.py, modules/export_engine.py,
modules/tts_engine_manager.py, modules/image_processor.py,
core/core_engine.py, core/cache_service.py.

## PHASE 9 — Defensive Programming & Reliability — 2026-07-28

Goal: make the application significantly harder to break — invalid
input, corrupted assets, missing dependencies, interrupted renders,
exhausted memory, locked files — WITHOUT changing a single audible or
visible characteristic produced by Phases 1-8. Nothing about audio
stability, voice effects, click removal, transition refinement,
hard-intro removal, paragraph narration, the pause planner, prosody,
subtitle sync, timestamps, timeline generation, scene timing or the
Phase 8 optimizations was touched — only what happens when something
goes wrong. Reliability only; no feature was redesigned.

NEW: `core/safe_io.py` — ONE source of truth for crash-safe filesystem
work, stdlib-only, in `core/` (like `time_helper`/`errors`/
`narration_pacing`) so every layer can share it without modules
importing each other (RULE 1). It provides atomic writes (unique
sibling temp file -> fsync -> `os.replace`), text/bytes/JSON wrappers,
tolerant JSON reads, corrupt-file quarantine, marker-scoped stale-temp
purging, and directory helpers that degrade to a writable fallback.

THE CORE PROBLEM IT SOLVES: Autopilot writes files that a LATER run
trusts — config, the cache index, processed frames, narration WAVs, SRT
sidecars, render checkpoints. Every one was written straight over its
destination, so an interruption at the wrong moment (power loss, a
killed render, a full disk, an antivirus lock) left a TRUNCATED file
that still `exists()` — and the resume/validation paths then treated it
as finished work. A reader now always sees either the complete old file
or the complete new one, and a failed write leaves the previous version
untouched. Windows sharing violations on `os.replace` are retried
briefly instead of failing a whole export at its last step.

ATOMIC NOW: app_settings/config (also fixing a FIXED `.tmp` name two
threads could interleave into), the cache index and every cache entry,
`_write_audio` and the assembled narration WAV, processed images and
proxies, SRT sidecars, YouTube metadata, Drive resume state, the
pronunciation dictionary, storyboard PDFs, and database backups.

FFMPEG / PROCESS CLEANUP. `_run_ffmpeg` now ALWAYS reaps its child
(terminate -> kill -> wait): a timeout previously killed without
waiting, leaving a zombie holding the output file open — which on
Windows also blocked the following `os.replace` of the muxed file — and
a cancel between spawn and wait leaked the process entirely. `process`
could also be unbound in the handler when `Popen` itself raised, so the
error path raised `UnboundLocalError`. A bounded stderr tail is kept
and reported, so a non-zero exit now says WHY ffmpeg failed. Partial
outputs from a failed scene render are discarded (a truncated segment
would otherwise be mistaken for resumable work), and orphaned
`.groupN.mp4` / `.mux.mp4` temporaries are cleaned up on every path.

DATABASE SAFETY. Transient "database is locked" is retried with backoff
(real errors still raise immediately and unchanged); a broken
connection is discarded so the next call reconnects instead of failing
for the rest of the session; PRAGMAs apply best-effort (WAL is
unsupported on some network shares — a reason to use the default
journal, not to fail every query); `transaction()` gives all-or-nothing
multi-statement writes and `_persist_scenes_and_lines` uses it, so a
failure part-way can no longer leave a project holding HALF a script
that every later stage treats as complete; backups are atomic; and
`close_all()` releases connections opened on render-pool threads (which
otherwise keep WAL handles — and on Windows a file lock — open).

CORRUPT-STATE RECOVERY. A corrupt cache index is quarantined once
instead of being re-read and re-failed every launch, and malformed
entries are dropped rather than raising from inside `get`. Corrupt
config falls back to the DOCUMENTED default shape (not a bare `{}`),
and wrong-typed JSON is caught too. Corrupt `render_progress` JSON
columns degrade per column, so a crash-damaged checkpoint can still be
resumed or cleanly restarted instead of failing the recovery dialog
with a raw parser error. A corrupt `sfx_config.json` degrades to an
empty catalog (SFX is optional garnish, never a reason to fail).

CANCELLATION & MEMORY. Cancelling used to appear to hang: returning
from inside a `with ThreadPoolExecutor` still blocks until every queued
unit finishes, so a long script kept synthesizing/rendering for minutes.
Queued-but-unstarted work is now cancelled and the pool drains promptly,
while in-flight work is still awaited (killing a thread mid-write is
what leaves corrupt WAVs). `MemoryError` is handled per stage: buffers
are released and a clear, actionable message is returned instead of an
unhandled crash. Ctrl-C is treated as a cancel, not a crash, and the
decoded-audio memo is released in a `finally` (previously only on the
two mix SUCCESS paths, so a failed mix held up to 256MB for the rest of
the render).

DEGRADATION & VALIDATION. Log and cache folders fall back to the OS
temp area rather than blocking boot on a read-only install location;
a log file that can't be opened leaves console logging working.
FFmpeg/ffprobe discovery requires a real FILE (an empty or
directory-valued hint used to pass `exists()` and be handed to
subprocess as the executable). Image validation forces a real decode
via `load()`. TTS gains one bounded synthesis retry before the audible
synthetic-tone fallback, and the PHASE 1 revert-on-failure protection
no longer dies when the BACKUP read itself fails. Batch retry recursion
is depth-capped so a corrupt `max_retries` row can't exhaust the stack.

BACKWARD COMPATIBILITY: no public API changed, no config key added or
changed, no schema or file-format change, no migration. Existing
projects and settings files work untouched. Every new path is a
failure path — on the success path the bytes written and the order of
operations are exactly as before.

Gates: py_compile clean (all files); full-package import sweep clean
(50/50 non-Qt modules; the 18 PyQt6 failures are identical to the
baseline and are absent-dependency only); all 23 config JSON valid;
PyInstaller spec parses and `core.safe_io` is added to `_CORE_MODULES`
so the frozen exe embeds it; production container boots and loads
20/20 modules + the example plugin; no new lint findings (per-file,
per-rule profile IDENTICAL to baseline); no added line >88 cols; no
trailing whitespace, dead code, debug output or TODOs. Suite: 757
passed / 26 failed / 6 skipped — failure set BIT-IDENTICAL to the
pre-change baseline captured from unmodified main in the same sandbox
(every failure pre-existing: optional deps absent). ZERO regressions.
Functional QA and QA renders are performed by the user.

Files: core/safe_io.py (new), core/cache_service.py,
core/config_service.py, core/core_engine.py, core/database_service.py,
core/hardware_service.py, core/log_service.py,
modules/audio_processor.py, modules/batch_engine.py,
modules/drive_upload_engine.py, modules/export_engine.py,
modules/image_processor.py, modules/sfx_engine.py,
modules/subtitle_engine.py, modules/tts_engine_manager.py,
ui/viewmodel.py, main.py, build/autopilot.spec, build_manifest.json.
