# Building Autopilot.exe (Phase D.5)

## What you get

`dist\Autopilot\Autopilot.exe` — a one-folder PyInstaller bundle:

```
dist\Autopilot\
  Autopilot.exe        ← double-click = CLI help; runs all commands
  config\              ← all engine configs + modules registry
  database\schema.sql  ← first boot creates autopilot.db beside it
  assets\, channel_profiles\
  _internal\           ← Python runtime + bundled libraries
```

ONEDIR is deliberate (NOT a single .exe): the app expects a writable
project root — database, logs, temp, engines — next to the binary.
Zip the whole `dist\Autopilot\` folder to ship to another machine.

## Build (Windows)

```bat
setup.bat                              REM once: venv + core deps
pip install -r requirements_ui.txt     REM optional: UI-enabled exe
scripts\build_exe.bat
```

The script ends with a self-check: the frozen app runs
`Autopilot.exe modules` and must list all 20 registry modules.

## Provision engines (same as dev runs)

The bundle ships **no binaries** (license/size and because tests
never touch them):

```bat
mkdir dist\Autopilot\engines\ffmpeg
copy C:\path\to\ffmpeg.exe  dist\Autopilot\engines\ffmpeg\
copy C:\path\to\ffprobe.exe dist\Autopilot\engines\ffmpeg\
```

Without them everything still boots; quality-gate warns and render
stops gracefully at the export stage (RULE 7 behaviour, same as CLI).

## Run

```bat
dist\Autopilot\Autopilot.exe render --script my_script.txt --images my_images --project-folder out
dist\Autopilot\Autopilot.exe render-project <ID>
dist\Autopilot\Autopilot.exe ui              (if built with PyQt6)
dist\Autopilot\Autopilot.exe license --activate XXXX-XXXX-XXXX-XXXX
```

## Notes / limitations (D.5 honest list)

* Built on Windows for Windows (PyInstaller does not cross-compile).
* UPX off on purpose: smaller AV-false-positive surface.
* Console stays attached even when using the UI (acceptable for D.5;
  flip `console=False` in build/autopilot.spec for a silent GUI build).
* License is per-machine (HWID) — activating in the dev folder does not
  activate the dist copy; the trial restarts there (by design).
* First boot in dist creates database\autopilot.db + logs\ + temp\.
