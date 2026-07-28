#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi
export PYTHONPATH=.
black --check core tests || true
ruff check core tests || true
mypy core --ignore-missing-imports || true
pytest tests -v --tb=short
