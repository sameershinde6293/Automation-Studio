#!/usr/bin/env bash
# Run the same checks as the CI pipeline, locally.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Backend: install + test + coverage"
cd backend
if [ ! -d venv ]; then
  python3 -m venv venv
fi
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
./venv/bin/pip install -q pytest-cov
./venv/bin/python -m pytest --cov=app --cov-report=term-missing
cd "$ROOT"

echo "==> Frontend: install + build + test"
cd frontend
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
npm install --no-audit --no-fund
npm run build
if npm run | grep -q "test:run"; then
  npm run test:run
fi
cd "$ROOT"

echo "==> All CI checks passed."
