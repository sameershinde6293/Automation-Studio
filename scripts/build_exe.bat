@echo off
REM Autopilot D.5 build — PyInstaller one-folder bundle (Windows).
REM Produces dist\Autopilot\Autopilot.exe and runs a boot self-check.
REM Prereqs: run setup.bat first (venv + core deps). For a UI-enabled
REM exe: pip install -r requirements_ui.txt BEFORE this script.
cd /d "%~dp0\.."
SETLOCAL ENABLEEXTENSIONS

IF EXIST venv\Scripts\activate.bat call venv\Scripts\activate.bat

echo [Autopilot] Installing PyInstaller...
python -m pip install "pyinstaller>=6.0,<7"
if errorlevel 1 (
  echo [Autopilot] ERROR: could not install PyInstaller.
  exit /b 1
)

echo [Autopilot] Cleaning previous dist/build...
if exist dist\Autopilot rmdir /s /q dist\Autopilot
if exist build\Autopilot rmdir /s /q build\Autopilot

echo [Autopilot] Building (this takes a few minutes)...
python -m PyInstaller --noconfirm --clean --log-level WARN build\autopilot.spec
if errorlevel 1 (
  echo [Autopilot] ERROR: PyInstaller failed — see warnings above.
  exit /b 1
)

echo [Autopilot] Self-check: frozen app boots and lists modules...
dist\Autopilot\Autopilot.exe modules
if errorlevel 1 (
  echo [Autopilot] ERROR: frozen app self-check failed.
  exit /b 1
)

echo.
echo [Autopilot] BUILD OK: dist\Autopilot\Autopilot.exe
echo   Next: copy ffmpeg.exe + ffprobe.exe into dist\Autopilot\engines\ffmpeg\
echo   then: dist\Autopilot\Autopilot.exe render --script ... --images ...
echo   UI:   dist\Autopilot\Autopilot.exe ui  (needs PyQt6 at build time)
exit /b 0
