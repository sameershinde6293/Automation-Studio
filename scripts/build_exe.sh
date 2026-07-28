#!/usr/bin/env bash
# Autopilot D.5 build — PyInstaller one-folder bundle (POSIX twin of
# build_exe.bat; also the sandbox milestone test for the D.5 spec).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[Autopilot] Installing PyInstaller..."
python3 -m pip install --quiet --user "pyinstaller>=6.0,<7"

echo "[Autopilot] Cleaning previous dist/build..."
rm -rf dist/Autopilot build/Autopilot

echo "[Autopilot] Building..."
python3 -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath "${AUTOPILOT_DIST:-dist}" --workpath "${AUTOPILOT_WORK:-build}" \
  build/autopilot.spec

echo "[Autopilot] Self-check: frozen app boots and lists modules..."
"${AUTOPILOT_DIST:-dist}/Autopilot/Autopilot" modules

echo "[Autopilot] BUILD OK: ${AUTOPILOT_DIST:-dist}/Autopilot/Autopilot"
