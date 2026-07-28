#!/usr/bin/env bash
# M8: Rollback script - restores previous version and schema
# See docs/DEPLOYMENT.md

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== Creator OS Rollback (M8) ==="
echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

if [ $# -lt 1 ]; then
    echo "Usage: $0 <previous-commit-or-tag>"
    echo "Example: $0 HEAD~1  (rollback one commit)"
    echo "Example: $0 v1.1.0"
    echo "Example: $0 main (if you want to rollback migration only, use alembic downgrade)"
    echo ""
    echo "For migration-only rollback:"
    echo "  Docker: docker compose --profile tools run --rm migrate alembic downgrade -1"
    echo "  Source: cd backend && .venv/bin/alembic downgrade -1"
    exit 1
fi

TARGET="$1"

echo "Rolling back to: $TARGET"
echo "Current: $(git rev-parse HEAD --short) $(git log -1 --oneline)"

# Backup before rollback
echo ""
echo "--- Pre-rollback backup ---"
./scripts/backup.sh "./backups/pre-rollback-$(date +%Y%m%d-%H%M%S)" || echo "⚠ Backup failed"

read -p "Proceed with rollback to $TARGET? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted"
    exit 1
fi

# Show what will change
echo ""
echo "--- Changes that will be reverted ---"
git log --oneline "$TARGET"..HEAD -20 || true

echo ""
echo "--- Schema history ---"
if command -v docker >/dev/null 2>&1; then
    # For docker, we need to check migration chain
    echo "Use: docker compose --profile tools run --rm migrate alembic history | tail -20"
else
    cd backend
    .venv/bin/alembic history | tail -20
    cd ..
fi

read -p "Confirm rollback (code + schema)? (yes/no): " CONFIRM2
if [ "$CONFIRM2" != "yes" ]; then
    echo "Aborted"
    exit 1
fi

if command -v docker >/dev/null 2>&1 && [ -f docker-compose.yml ]; then
    echo ""
    echo "=== Docker rollback ==="

    echo "1. Rolling back schema FIRST (safer to have old code with new schema than new code with old schema?)"
    echo "   Actually for rollback: new code still running, so downgrade schema after stopping code is safer"
    echo "   But documented procedure is to downgrade schema before starting old code"
    echo ""
    echo "   Downgrading one revision (use -1, or base for full)"
    read -p "Downgrade schema? (yes/no/skip): " DO_DOWNGRADE
    if [ "$DO_DOWNGRADE" = "yes" ]; then
        docker compose --profile tools run --rm migrate alembic downgrade -1
        echo "✓ Schema downgraded -1"
    elif [ "$DO_DOWNGRADE" = "skip" ]; then
        echo "Skipping schema downgrade"
    else
        echo "Enter specific downgrade target (e.g., -1, base, <revision>):"
        read DOWNGRADE_TARGET
        docker compose --profile tools run --rm migrate alembic downgrade "$DOWNGRADE_TARGET"
    fi

    echo ""
    echo "2. Checking out old code..."
    git checkout "$TARGET" -- backend frontend docker-compose.yml .env.production.example || git checkout "$TARGET"

    echo ""
    echo "3. Rebuilding old images..."
    docker compose build

    echo ""
    echo "4. Restarting with old code..."
    docker compose up -d

    echo ""
    echo "5. Verifying..."
    for i in {1..20}; do
        if curl -fsS http://localhost:${HTTP_PORT:-8080}/health/ready > /dev/null 2>&1; then
            echo "✓ Ready"
            break
        fi
        sleep 2
    done
    docker compose ps

else
    echo ""
    echo "=== Source rollback ==="

    echo "1. Downgrading schema (optional)..."
    cd backend
    echo "Current head: $(.venv/bin/alembic current)"
    read -p "Downgrade? (yes/no/skip): " DO_DOWNGRADE
    if [ "$DO_DOWNGRADE" = "yes" ]; then
        .venv/bin/alembic downgrade -1
    fi

    cd ..

    echo ""
    echo "2. Checking out old code..."
    git checkout "$TARGET" -- backend frontend || git checkout "$TARGET"

    echo ""
    echo "3. Reinstalling dependencies..."
    cd backend
    .venv/bin/pip install -r requirements.txt
    cd ../frontend
    npm ci --no-audit --no-fund
    npx tsc && npx vite build
    cd ..

    echo ""
    echo "4. Restart backend manually"
fi

echo ""
echo "=== Rollback completed ==="
echo "Verify: curl /health/ready"
echo "If rollback failed, restore from backup: ./scripts/restore.sh ./backups/pre-rollback-..."

# Note: leaving in detached HEAD or rolled back state is intentional
# Operator should decide whether to stay or move forward
