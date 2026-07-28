# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — Autopilot (Phase D.5).

Design decisions (D.5):
* ONEDIR (COLLECT) output, NOT onefile: at runtime the app treats the
  executable's parent folder as the project root and expects config/,
  database/schema.sql, engines/ (user-provisioned binaries), database
  (writable), logs/ (writable) beside it. A onefile build would unpack
  to a temp _MEIPASS dir and break main.find_project_root().
* modules/* are loaded dynamically via importlib by core_engine's
  registry loader, so PyInstaller's static analysis cannot see them —
  every registry module + core service is listed in hiddenimports.
* Third-party packages that modules import lazily inside functions
  (pydub, thefuzz, pdfplumber, chardet, cryptography, soundfile, docx)
  are hidden for the same reason.
* PyQt6 is CONDITIONAL: when installed at build time, ui.app is
  embedded and `Autopilot.exe ui` opens the window; when absent, the
  UI command degrades to the CLI hint exactly as tested. Never fails
  the build either way.
* tests/, pytest, and dev tooling are excluded from the bundle.
"""

import importlib.util
from pathlib import Path

block_cipher = None
APP_ROOT = Path(SPECPATH).parent  # repo root (spec lives in build/)

_REGISTRY_MODULES = [
    "file_parser", "tts_engine_manager", "voice_profile_manager",
    "image_processor", "audio_processor", "subtitle_engine",
    "timeline_engine", "transition_engine", "animation_engine",
    "color_grade_engine", "sfx_engine", "intro_outro_engine",
    "export_engine", "thumbnail_generator", "batch_engine",
    "quality_checker", "channel_profile_manager", "voice_store_manager",
    "keyword_analyzer", "drive_upload_engine",
]
_CORE_MODULES = [
    "cache_service", "config_service", "core_engine", "correlation",
    "database_service", "errors", "event_bus", "hardware_service",
    "license_manager", "log_service", "narration_pacing", "narration_prosody",
    "render_state_machine", "safe_io", "service_container", "time_helper",
]
_LAZY_THIRD_PARTY = [
    "pydub", "thefuzz", "Levenshtein", "pdfplumber", "pdfminer",
    "chardet", "cryptography", "soundfile", "docx", "requests",
    "psutil", "numpy", "PIL",
]

hiddenimports = (
    [f"modules.{name}" for name in _REGISTRY_MODULES]
    + [f"core.{name}" for name in _CORE_MODULES]
    + ["ui", "ui.viewmodel"]
    + _LAZY_THIRD_PARTY
)

has_pyqt = importlib.util.find_spec("PyQt6") is not None
if has_pyqt:  # embed the UI shell only when Qt exists at build time
    hiddenimports.append("ui.app")

datas = [
    (str(APP_ROOT / "config"), "config"),
    # D.8: the shipped example plugin; users add more beside it in the
    # onedir (_internal/plugins) - plugins load by FILE PATH, so no
    # hiddenimport or rebuild is ever needed for user plugins.
    (str(APP_ROOT / "plugins"), "plugins"),
    (str(APP_ROOT / "database" / "schema.sql"), "database"),
    (str(APP_ROOT / "assets"), "assets"),
    (str(APP_ROOT / "channel_profiles"), "channel_profiles"),
    (str(APP_ROOT / "requirements.txt"), "."),
    (str(APP_ROOT / "requirements_ui.txt"), "."),
    (str(APP_ROOT / "README.md"), "."),
]

excludes = [
    "pytest", "_pytest", "tests", "tkinter", "matplotlib",
    "nuitka", "PyQt5", "PySide2", "PySide6",
]

a = Analysis(
    [str(APP_ROOT / "main.py")],
    pathex=[str(APP_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Autopilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # AV false-positives + slower startup; keep honest PE
    console=True,  # CLI-first app; GUI window opens from 'ui' command
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Autopilot",
)
