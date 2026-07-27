#!/usr/bin/env bash
# M8: Upgrade script - order matters! Schema first, then code
# See docs/DEPLOYMENT.md and docs/UPGRADE_GUIDE.md

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== Creator OS Upgrade (M8) ==="
echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Current commit: $(git rev-parse HEAD)"
echo ""

# Check for uncommitted changes
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "⚠ Uncommitted changes detected"
    git status --short
    read -p "Continue anyway? (yes/no): " CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        exit 1
    fi
fi

# Backup before upgrade
echo "--- Pre-upgrade backup ---"
./scripts/backup.sh "./backups/pre-upgrade-$(date +%Y%m%d-%H%M%S)" || echo "⚠ Backup failed, continuing anyway"

echo ""
echo "--- Pulling latest changes ---"
git fetch origin
git log --oneline HEAD..origin/main -10 || echo "No new commits or not tracking origin/main"

read -p "Proceed with upgrade? This will pull and rebuild. (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted"
    exit 1
fi

# If docker available, use docker upgrade path
if command -v docker >/dev/null 2>&1 && [ -f docker-compose.yml ]; then
    echo ""
    echo "=== Docker deployment upgrade ==="
    echo "Order: schema migration FIRST, then code (to avoid new code running against old schema)"
    echo ""
    echo "1. Pulling new code..."
    git pull --ff-only || git pull

    echo ""
    echo "2. Building new images..."
    docker compose build

    echo ""
    echo "3. Running migrations (schema FIRST)..."
    docker compose --profile tools run --rm migrate

    echo ""
    echo "4. Restarting stack with new code..."
    docker compose up -d

    echo ""
    echo "5. Verifying health..."
    for i in {1..30}; do
        if curl -fsS http://localhost:${HTTP_PORT:-8080}/health/ready > /dev/null 2>&1; then
            echo "✓ Ready after $i attempts"
            curl -fsS http://localhost:${HTTP_PORT:-8080}/health/ready | python3 -m json.tool
            break
        fi
        echo "Waiting $i..."
        sleep 2
    done

    echo ""
    echo "=== Upgrade completed ==="
    docker compose ps
    docker compose logs --tail 50 backend

else
    echo ""
    echo "=== Source deployment upgrade ==="

    echo "1. Pulling new code..."
    git pull --ff-only || git pull

    echo ""
    echo "2. Installing backend dependencies..."
    cd backend
    .venv/bin/pip install -r requirements.txt

    echo ""
    echo "3. Running migrations..."
    .venv/bin/alembic upgrade head

    echo ""
    echo "4. Frontend build..."
    cd ../frontend
    npm ci --no-audit --no-fund
    npx tsc && npx vite build

    cd ..

    echo ""
    echo "5. Restart backend (if using systemd, run: sudo systemctl restart creator-os)"
    echo "   Or manually: pkill -f uvicorn && cd backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
    echo ""
    echo "=== Upgrade completed ==="
    echo "Verify: curl http://localhost:8000/health/ready"
fi

echo ""
echo "Post-upgrade verification:"
echo "  - Check version: curl /api/system/info or /health"
echo "  - Check validation: curl -H \"Authorization: Bearer <token>\" /api/system/config/validation"
echo "  - Run tests: ./scripts/ci-local.sh"
echo "  - Verify examples: python scripts/verify_examples.py"
