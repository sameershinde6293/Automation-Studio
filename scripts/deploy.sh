#!/usr/bin/env bash
# M8: Production deployment script for Creator OS
# Supports docker-compose deployment with validation steps

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== Creator OS Production Deployment (M8) ==="
echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Commit: $(git rev-parse HEAD 2>/dev/null || echo unknown)"
echo ""

# Check prerequisites
echo "--- Checking prerequisites ---"
if ! command -v docker >/dev/null 2>&1; then
    echo "❌ Docker not found"
    echo "This deployment method requires Docker Engine 24+ and Compose v2"
    echo ""
    echo "Alternative: source-based deployment"
    echo "  cp .env.production.example .env"
    echo "  # edit .env"
    echo "  cd backend && .venv/bin/alembic upgrade head && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
    echo ""
    echo "See docs/DEPLOYMENT.md for full procedure"
    exit 1
fi

echo "✓ Docker: $(docker --version)"
echo "✓ Compose: $(docker compose version)"

if [ ! -f .env ]; then
    echo "❌ .env not found"
    echo "  cp .env.production.example .env"
    echo "  Edit .env: set AUTH_SECRET_KEY and POSTGRES_PASSWORD (mandatory)"
    exit 1
fi

echo "✓ .env exists"

# Validate mandatory secrets
if grep -q "^AUTH_SECRET_KEY=$" .env || ! grep -q "^AUTH_SECRET_KEY=" .env || grep -q "^AUTH_SECRET_KEY= *$" .env; then
    echo "❌ AUTH_SECRET_KEY not set in .env"
    echo "Generate with: python3 -c \"import secrets; print(secrets.token_urlsafe(48))\""
    exit 1
fi

if grep -q "^POSTGRES_PASSWORD=$" .env || ! grep -q "^POSTGRES_PASSWORD=" .env; then
    echo "❌ POSTGRES_PASSWORD not set in .env"
    echo "Generate with: python3 -c \"import secrets; print(secrets.token_urlsafe(24))\""
    exit 1
fi

echo "✓ Mandatory secrets present"

# Validate compose file
echo ""
echo "--- Validating compose file ---"
docker compose config > /dev/null
echo "✓ docker-compose.yml valid"

# Pull base images
echo ""
echo "--- Pulling base images ---"
docker compose pull db || echo "⚠ Could not pull db image (may be offline)"

# Build
echo ""
echo "--- Building images ---"
docker compose build --pull || docker compose build

echo "✓ Images built"

# Migrate (one-shot)
echo ""
echo "--- Running migrations (one-shot) ---"
docker compose --profile tools run --rm migrate
echo "✓ Migrations applied"

# Startup
echo ""
echo "--- Starting stack ---"
docker compose up -d

echo "Waiting for healthchecks (60s timeout)..."
for i in {1..12}; do
    if docker compose ps --format json | python3 -c "import sys, json; data = [json.loads(l) for l in sys.stdin]; healthy = sum(1 for s in data if 'healthy' in s.get('Status','') or 'healthy' in s.get('State','')); print(healthy)" 2>/dev/null | grep -q "2"; then
        echo "✓ At least 2 services healthy"
        break
    fi
    echo "  Waiting... $((i*5))s"
    sleep 5
done

docker compose ps

echo ""
echo "--- Health verification ---"
echo "Waiting for backend to be ready..."
for i in {1..30}; do
    if curl -fsS http://localhost:${HTTP_PORT:-8080}/health/live > /dev/null 2>&1; then
        echo "✓ Liveness: ok after $i attempts"
        break
    fi
    sleep 2
done

curl -fsS http://localhost:${HTTP_PORT:-8080}/health/live | python3 -m json.tool
curl -fsS http://localhost:${HTTP_PORT:-8080}/health/ready | python3 -m json.tool || (echo "❌ Readiness failed" && docker compose logs backend && exit 1)

echo ""
echo "--- Security posture checks ---"
# /docs should be 404 in production
STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:${HTTP_PORT:-8080}/docs || true)
if [ "$STATUS" = "404" ]; then
    echo "✓ /docs returns 404 (not exposed in production)"
else
    echo "⚠ /docs returns $STATUS (expected 404, check ENABLE_DOCS=false)"
fi

# Unauthenticated API should be 401
STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:${HTTP_PORT:-8080}/api/workflows/ || true)
if [ "$STATUS" = "401" ] || [ "$STATUS" = "403" ]; then
    echo "✓ Unauthenticated API returns $STATUS (auth enforced)"
else
    echo "⚠ Unauthenticated API returns $STATUS (expected 401/403)"
fi

echo ""
echo "--- Observability ---"
curl -fsS http://localhost:${HTTP_PORT:-8080}/metrics | head -20
echo "✓ Metrics reachable"

echo ""
echo "=== Deployment successful ==="
echo "URLs:"
echo "  Frontend: http://localhost:${HTTP_PORT:-8080}/"
echo "  Health: http://localhost:${HTTP_PORT:-8080}/health/ready"
echo "  Metrics: http://localhost:${HTTP_PORT:-8080}/metrics"
echo ""
echo "Next steps:"
echo "  - Create first admin: set AUTH_BOOTSTRAP_USERNAME/PASSWORD in .env, then docker compose up -d backend"
echo "  - Configure TLS termination: see deploy/nginx/creator-os.conf"
echo "  - Verify backups: ./scripts/backup.sh"
echo "  - Monitor: curl /health/ready and /metrics"
echo ""
echo "To stop: docker compose down"
echo "To stop and remove volumes (DESTRUCTIVE): docker compose down -v"
