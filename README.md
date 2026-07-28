# Autopilot

Documentary video automation software for faceless YouTube creators.

**Version:** 1.0.0  
**Phase:** D SHIPPED — 544 tests green on Windows, CLI + PyQt6 UI + standalone Autopilot.exe all render real MP4s (6/6 gate)

## What it does

Upload a script, images, and optional music. Autopilot generates narration, timeline, color grade, transitions, subtitles, and a YouTube-ready MP4 — fully offline after setup.

## Quick start

```bat
setup.bat
pip install -r requirements_ui.txt   REM optional, for the PyQt6 window
scripts\build_exe.bat                REM optional: dist\Autopilot\Autopilot.exe
```

Render a video (needs ffmpeg.exe + ffprobe.exe in engines\ffmpeg\):

```bat
python main.py render --script tests\fixtures\sample_project\script\sample_script.txt --images tests\fixtures\sample_project\images --project-folder smoke_out
python main.py ui
```

See `docs\BUILD_EXE.md` for the exe, `PROJECT_STATE.md` for full status.

## Status snapshot (Phase D.8/D.9)

- 20 engine modules + 13 core services behind the core_engine orchestrator
- Optional Google Drive backup stage (resumable, OFF by default -- docs/DRIVE_UPLOAD.md)
- User plugin interface (plugins/ + CLI `plugin`; docs/PLUGINS.md)
- Inno Setup installer script (scripts/build_installer.bat; docs/INSTALLER.md)
- Full CLI: render / render-project / check / batch / modules / license / ui
- PyQt6 main window (optional dependency)
- 566-test suite green in sandbox, both execution modes (last Windows gate: 551/4/0, D.6)
- See docs/BUILD_EXE.md for producing Autopilot.exe

## Quick start (dev)

```bash
# Linux/macOS
bash scripts/setup.sh
bash scripts/test.sh

# Windows
setup.bat
test.bat
```

## Admin tool (Phase C)

- Admin password: `IAMKING`
- Admin hint: `IKNG`

## Architecture

Layered design: UI → Core Engine → Service Container → Modules → Infrastructure.  
Modules never import each other; they receive services via `BaseModule`.

## License

Proprietary commercial software. All rights reserved.
