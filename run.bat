@echo off
REM Autopilot launcher (Windows)
cd /d "%~dp0"
IF EXIST venv\Scripts\activate.bat (
  call venv\Scripts\activate.bat
)
python main.py
