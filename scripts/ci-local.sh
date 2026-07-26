#!/usr/bin/env bash
# Run the same checks as the CI pipeline, locally.
#
# CI itself has never executed (see ci/README.md — it needs a maintainer to
# move the workflow into .github/workflows/), so this script is currently the
# only way these checks run automatically.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Backend: install + test + coverage"
cd backend
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
./.venv/bin/python -m pytest -q --cov=app --cov-report=term-missing
cd "$ROOT"

echo "==> Backend: migration round-trip"
cd backend
./.venv/bin/python -m pytest -q tests/m5/test_migrations_m5.py
cd "$ROOT"

echo "==> Frontend: install + typecheck + build + test"
cd frontend
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
npm install --no-audit --no-fund
npm run typecheck
npx tsc && npx vite build
npm test
cd "$ROOT"

echo "==> All CI checks passed."
