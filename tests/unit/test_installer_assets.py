"""Static checks for the D.9 Inno Setup installer assets.

Inno Setup (ISCC.exe) is Windows-only, so the sandbox cannot COMPILE
the installer. These tests pin everything compilable in text: required
sections/flags for the onedir layout, the version contract with
main.py (bump APP_VERSION without the .iss and this fails loudly),
source-path references, and the companion script/doc files.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ISS = ROOT / "installer" / "autopilot_setup.iss"
BAT = ROOT / "scripts" / "build_installer.bat"
TXT = ROOT / "installer" / "AFTER_INSTALL.txt"
DOC = ROOT / "docs" / "INSTALLER.md"


def _iss() -> str:
    return ISS.read_text(encoding="utf-8")


def test_installer_files_exist() -> None:
    for path in (ISS, BAT, TXT, DOC):
        assert path.is_file(), path


def test_iss_required_sections_and_flags() -> None:
    text = _iss()
    for section in ("[Setup]", "[Files]", "[Icons]", "[Tasks]",
                    "[Run]", "[Dirs]"):
        assert section in text
    assert "ignoreversion recursesubdirs createallsubdirs" in text
    assert "ArchitecturesAllowed=x64compatible" in text
    assert 'SourceDir "..\\dist\\Autopilot"' in text
    assert "Autopilot.exe" in text
    assert re.search(r"AppId=\{\{[0-9A-F-]{36}\}", text)


def test_iss_version_matches_main_app_version() -> None:
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION = "(\d+\.\d+\.\d+)"', main)
    assert match, "APP_VERSION not found in main.py"
    version = match.group(1)
    assert f'#define AppVersion "{version}"' in _iss()
    assert f"AutopilotSetup-{version}" in _iss()
    bat = BAT.read_text(encoding="utf-8")
    assert f"AutopilotSetup-{version}.exe" in bat


def test_bat_finds_iscc_and_checks_dist() -> None:
    bat = BAT.read_text(encoding="utf-8")
    assert "ISCC.exe" in bat
    assert "dist\\Autopilot\\Autopilot.exe" in bat
    assert "installer\\autopilot_setup.iss" in bat
    assert "build_exe.bat" in bat


def test_after_install_warns_about_ffmpeg() -> None:
    text = TXT.read_text(encoding="utf-8").lower()
    assert "ffmpeg" in text
    assert "engines\\ffmpeg" in text
    assert "_internal" in text
