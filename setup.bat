@echo off
REM Autopilot setup script (Windows)
REM Installs CORE dependencies only (no Coqui TTS / no numpy pin war)
SETLOCAL ENABLEEXTENSIONS
cd /d "%~dp0"

echo [Autopilot] Working directory: %CD%
echo [Autopilot] Creating virtual environment...
python -m venv venv
if errorlevel 1 (
  echo [Autopilot] ERROR: Failed to create venv. Is Python on PATH?
  exit /b 1
)
call venv\Scripts\activate.bat

echo [Autopilot] Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 (
  echo [Autopilot] ERROR: pip upgrade failed.
  exit /b 1
)

echo [Autopilot] Installing CORE dependencies from requirements.txt...
echo [Autopilot] NOTE: Coqui XTTS is NOT installed by default (numpy conflict).
echo [Autopilot]       Optional later: see requirements_tts_optional.txt
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [Autopilot] ERROR: pip install failed.
  echo [Autopilot] Try: python -m pip install -r requirements.txt -v
  exit /b 1
)

echo [Autopilot] Verifying critical packages...
python -c "import PIL, numpy, pytest, pydub; print('PIL', PIL.__version__); print('numpy', numpy.__version__); print('pytest OK')"
if errorlevel 1 (
  echo [Autopilot] ERROR: Critical imports failed after install.
  exit /b 1
)

echo [Autopilot] Generating fixtures...
set PYTHONPATH=%CD%
python tests\fixtures\generate_fixtures.py
if errorlevel 1 (
  echo [Autopilot] ERROR: Fixture generation failed.
  exit /b 1
)

echo [Autopilot] Initializing database...
python -c "from core.database_service import SQLiteDatabase, DatabaseService; db=SQLiteDatabase('database/autopilot.db','database/schema.sql'); print('init', db.initialize()); svc=DatabaseService(db); print(svc.verify_product_tables())"
if errorlevel 1 (
  echo [Autopilot] ERROR: Database init failed.
  exit /b 1
)

echo.
echo [Autopilot] Setup complete.
echo [Autopilot] Next:
echo   1. venv\Scripts\activate
echo   2. set PYTHONPATH=%%CD%%
echo   3. pytest tests -q --tb=short
echo.
echo [Autopilot] Optional FFmpeg: place ffmpeg.exe in engines\ffmpeg\
echo [Autopilot] Optional XTTS: see requirements_tts_optional.txt
ENDLOCAL
