@echo off
REM Build the Autopilot installer (Phase D.9) with Inno Setup 6.
REM Requires dist\Autopilot\Autopilot.exe (run scripts\build_exe.bat first).
setlocal EnableExtensions
cd /d "%~dp0\.."

if not exist "dist\Autopilot\Autopilot.exe" (
    echo [FAIL] dist\Autopilot\Autopilot.exe not found.
    echo        Run scripts\build_exe.bat first, then re-run this script.
    exit /b 1
)

set "ISCC="
where ISCC.exe >nul 2>nul && set "ISCC=ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" (
    set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
)
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" (
    set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
)
if not defined ISCC (
    echo [FAIL] Inno Setup 6 ^(ISCC.exe^) not found.
    echo        Install from https://jrsoftware.org/isinfo.php ^(free^),
    echo        then re-run this script.
    exit /b 1
)

echo [build] compiling installer with "%ISCC%" ...
"%ISCC%" /Q "installer\autopilot_setup.iss"
if errorlevel 1 (
    echo [FAIL] Inno Setup compilation failed.
    exit /b 1
)

echo [ok] installer written to dist\installer\AutopilotSetup-3.1.0.exe
endlocal
