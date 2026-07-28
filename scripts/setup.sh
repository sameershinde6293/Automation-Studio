#!/usr/bin/env bash
# Autopilot setup script (Linux/macOS development hosts) — CORE deps only
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[Autopilot] Creating virtual environment..."
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate

echo "[Autopilot] Installing CORE dependencies (no Coqui TTS by default)..."
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "[Autopilot] Generating fixtures..."
export PYTHONPATH=.
python tests/fixtures/generate_fixtures.py

echo "[Autopilot] Initializing database..."
python - <<'PY'
from core.database_service import SQLiteDatabase, DatabaseService
db = SQLiteDatabase("database/autopilot.db", "database/schema.sql")
print("init", db.initialize())
svc = DatabaseService(db)
print("tables", svc.verify_product_tables())
print("integrity", svc.integrity_check())
PY

echo "[Autopilot] Setup complete."
