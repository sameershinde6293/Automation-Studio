@echo off
REM Autopilot build script (Windows) — Phase D.5
REM One-folder PyInstaller bundle: dist\Autopilot\Autopilot.exe
cd /d "%~dp0"
call scripts\build_exe.bat
