# Building the Windows Installer (D.9)

Prereqs on the build machine (Windows):

1. The frozen app: `scripts\build_exe.bat` → `dist\Autopilot\Autopilot.exe`
   (see `docs\BUILD_EXE.md`). Test it first (`Autopilot.exe modules`
   must list all 20 registry modules).
2. Inno Setup 6 (free): https://jrsoftware.org/isinfo.php — install with
   defaults; the build script finds `ISCC.exe` in Program Files or PATH.

Build:

```bat
scripts\build_installer.bat
```

Output: `dist\installer\AutopilotSetup-1.0.0.exe`

## What the installer does (installer\autopilot_setup.iss)

- Installs the full onedir tree to `%ProgramFiles%\Autopilot`
  (per-user install offered via the privileges dialog).
- Start Menu entries: **Autopilot** (launches the UI via
  `Autopilot.exe ui`) and a console entry; optional desktop icon
  (unchecked by default).
- Creates writable `projects\`, `logs\`, `cache\`, `temp\` folders.
- Final wizard page shows `AFTER_INSTALL.txt`: the **FFmpeg reminder**
  — copy `ffmpeg.exe`/`ffprobe.exe` into
  `<install>\_internal\engines\ffmpeg\`. FFmpeg is intentionally not
  bundled (GPL consideration + download size); every non-render
  feature runs without it.
- Registers an uninstaller (Add/Remove Programs entry with the app
  icon). User data in `database\` and `projects\` is NOT deleted on
  uninstall beyond the files the installer itself wrote
  (Inno default: only installed files are removed).

## Updating the version

`{#AppVersion}` in the .iss must match `APP_VERSION` in `main.py` and
`OutputBaseFilename` — the unit test
`tests/unit/test_installer_assets.py` enforces this, so bumping
`APP_VERSION` without updating the .iss fails the suite.

## Sandbox honesty note

Inno Setup is Windows-only; the compile step cannot run in the Linux
sandbox. What is verified by tests: script structure, required
sections/flags, version match with main.py, source-path references,
and the companion files. The first actual compile happens on the
user's Windows machine via `scripts\build_installer.bat`.
