#!/usr/bin/env bash
# Run the same checks as the CI pipeline, locally.
#
# CI itself has never executed before M8 (see ci/README.md — it needed a maintainer to
# move the workflow into .github/workflows/). In M8 we added .github/workflows/ci.yml
# to activate it.
#
# M8 adds: docker asset validation, observability tests, production checks, example verification
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

echo "==> Backend: M7 + M8 docker asset validation"
cd backend
./.venv/bin/python -m pytest -q tests/m7/test_docker_assets_m7.py tests/m8/
cd "$ROOT"

echo "==> Backend: observability (logging, metrics, health)"
cd backend
./.venv/bin/python -m pytest -q tests/m8/test_observability_m8.py tests/test_logger.py tests/m5/test_observability_m5.py
cd "$ROOT"

echo "==> Docker static validation"
./scripts/docker_validate.sh

echo "==> Production deployment check (source path)"
./scripts/production_check.sh

echo "==> Frontend: install + typecheck + build + test"
cd frontend
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
npm install --no-audit --no-fund
npm run typecheck
npx tsc && npx vite build
npm test
cd "$ROOT"

echo "==> Examples verification (requires backend running)"
echo "    Starting backend for example verification..."
cd backend
.venv/bin/alembic upgrade head -q
# M10-F1: SSL_CERT_FILE must be exported for the *backend*, not the verifier.
# The HTTP node runs server-side inside this process, so setting the variable
# on `verify_examples.py` (as M8/M9 documented) never reached the code making
# the outbound request — example 03 still failed CERTIFICATE_VERIFY_FAILED.
export SSL_CERT_FILE="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"
nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 > /tmp/ci-backend.log 2>&1 &
BACKEND_PID=$!
cd ..
for i in {1..20}; do
  if curl -fsS http://127.0.0.1:8000/health > /dev/null 2>&1; then
    echo "Backend ready after $i attempts"
    break
  fi
  sleep 1
done
python scripts/verify_examples.py || (cat /tmp/ci-backend.log && kill $BACKEND_PID || true; exit 1)
kill $BACKEND_PID || true
wait $BACKEND_PID || true

echo "==> Container runtime check (documents limitation if Docker absent)"
./scripts/container_validation.sh || true

echo "==> All CI checks passed."
