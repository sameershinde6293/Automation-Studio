#!/usr/bin/env bash
# M8: Docker asset validation (static, no runtime required)
# Validates Dockerfiles and docker-compose.yml for best practices,
# internal consistency, and production readiness.
#
# This runs without Docker installed - it parses files and checks conventions.
# For full validation with Docker, see container_validation.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

check_pass() {
    echo -e "${GREEN}✓${NC} $1"
    PASS=$((PASS+1))
}

check_fail() {
    echo -e "${RED}✗${NC} $1"
    FAIL=$((FAIL+1))
}

check_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
    WARN=$((WARN+1))
}

echo "=== M8 Docker Validation (Static) ==="
echo "Repo: $ROOT"
echo ""

# --------------------------------------------------------------------------- #
# Backend Dockerfile
# --------------------------------------------------------------------------- #
echo "--- Backend Dockerfile ---"
DOCKERFILE="$ROOT/backend/Dockerfile"

if [ ! -f "$DOCKERFILE" ]; then
    check_fail "backend/Dockerfile not found"
else
    # Multi-stage
    if grep -q "AS builder" "$DOCKERFILE" && grep -q "AS runtime" "$DOCKERFILE"; then
        check_pass "Multi-stage build (builder + runtime)"
    else
        check_fail "Multi-stage build missing (expected AS builder and AS runtime)"
    fi

    # Non-root USER
    if grep -q "^USER creator" "$DOCKERFILE"; then
        check_pass "Runs as unprivileged user (creator)"
    else
        check_fail "USER creator not found - image runs as root"
    fi

    # HEALTHCHECK uses liveness (no DB)
    if grep -q "HEALTHCHECK" "$DOCKERFILE" && grep -q "/health/live" "$DOCKERFILE"; then
        check_pass "HEALTHCHECK uses /health/live (liveness, no DB)"
    else
        check_fail "HEALTHCHECK missing or not using /health/live"
    fi

    # EXPOSE
    if grep -q "EXPOSE 8000" "$DOCKERFILE"; then
        check_pass "EXPOSE 8000"
    else
        check_fail "EXPOSE 8000 missing"
    fi

    # No secrets baked
    if grep -qi "AUTH_SECRET_KEY\|POSTGRES_PASSWORD" "$DOCKERFILE"; then
        check_fail "Potential secret baked in Dockerfile"
    else
        check_pass "No secrets baked in Dockerfile"
    fi

    # .dockerignore excludes .env
    if [ -f "$ROOT/backend/.dockerignore" ] && grep -q "\.env" "$ROOT/backend/.dockerignore"; then
        check_pass ".dockerignore excludes .env"
    else
        check_fail ".dockerignore missing or doesn't exclude .env"
    fi

    # Uses slim base
    if grep -q "python:3.11-slim" "$DOCKERFILE"; then
        check_pass "Uses python:3.11-slim (small base)"
    else
        check_warn "Base image not python:3.11-slim"
    fi

    # Has labels
    if grep -q "LABEL org.opencontainers.image" "$DOCKERFILE"; then
        check_pass "Has OCI labels"
    else
        check_warn "No OCI labels (recommended)"
    fi

    # Unbuffered Python
    if grep -q "PYTHONUNBUFFERED=1" "$DOCKERFILE"; then
        check_pass "PYTHONUNBUFFERED=1 (logs to stdout)"
    else
        check_warn "PYTHONUNBUFFERED not set"
    fi
fi

echo ""
echo "--- Frontend Dockerfile ---"
FRONTEND_DOCKERFILE="$ROOT/frontend/Dockerfile"

if [ ! -f "$FRONTEND_DOCKERFILE" ]; then
    check_fail "frontend/Dockerfile not found"
else
    if grep -q "AS builder" "$FRONTEND_DOCKERFILE" && grep -q "AS runtime" "$FRONTEND_DOCKERFILE"; then
        check_pass "Multi-stage build (builder + runtime)"
    else
        check_fail "Multi-stage build missing"
    fi

    if grep -q "HEALTHCHECK" "$FRONTEND_DOCKERFILE"; then
        check_pass "HEALTHCHECK present"
    else
        check_fail "HEALTHCHECK missing"
    fi

    if grep -q "nginx:.*-alpine" "$FRONTEND_DOCKERFILE"; then
        check_pass "Uses nginx:alpine (small)"
    else
        check_warn "Frontend base not nginx:alpine"
    fi

    if grep -q "node:.*-alpine" "$FRONTEND_DOCKERFILE"; then
        check_pass "Builder uses node:alpine"
    else
        check_warn "Builder not node:alpine"
    fi

    if grep -q "nginx -t" "$FRONTEND_DOCKERFILE"; then
        check_pass "Validates nginx config at build time (nginx -t)"
    else
        check_warn "No nginx -t validation"
    fi

    if [ -f "$ROOT/frontend/.dockerignore" ] && grep -q "node_modules" "$ROOT/frontend/.dockerignore"; then
        check_pass ".dockerignore excludes node_modules"
    else
        check_fail ".dockerignore missing node_modules"
    fi
fi

echo ""
echo "--- docker-compose.yml ---"
COMPOSE="$ROOT/docker-compose.yml"

if [ ! -f "$COMPOSE" ]; then
    check_fail "docker-compose.yml not found"
else
    # Services
    for svc in "db:" "migrate:" "backend:" "frontend:"; do
        if grep -q "^  $svc" "$COMPOSE"; then
            check_pass "Service $svc defined"
        else
            check_fail "Service $svc missing"
        fi
    done

    # Healthcheck for db
    if grep -A5 "db:" "$COMPOSE" | head -20; then
        :
    fi
    if grep -q "pg_isready" "$COMPOSE"; then
        check_pass "db healthcheck uses pg_isready (real readiness)"
    else
        check_fail "db healthcheck not using pg_isready"
    fi

    # Backend healthcheck uses readiness
    if grep -A30 "backend:" "$COMPOSE" | grep -q "/health/ready"; then
        check_pass "backend healthcheck uses /health/ready (readiness)"
    else
        check_fail "backend healthcheck not using /health/ready"
    fi

    # Restart policies
    if grep -q "restart: unless-stopped" "$COMPOSE"; then
        check_pass "Restart policy unless-stopped"
    else
        check_fail "Restart policy missing"
    fi

    # Volumes
    if grep -q "db_data:" "$COMPOSE" && grep -q "media_data:" "$COMPOSE"; then
        check_pass "Persistent volumes db_data and media_data"
    else
        check_fail "Persistent volumes missing"
    fi

    # No host publish for db
    # Check that db service block doesn't contain 5432:5432
    DB_BLOCK=$(sed -n '/^  db:/,/^  [a-z]*:/p' "$COMPOSE" | head -40)
    if echo "$DB_BLOCK" | grep -q "5432:5432"; then
        check_fail "db port published to host (security issue)"
    else
        check_pass "db not published to host (isolated network)"
    fi

    # Mandatory secrets use :?
    if grep -q "POSTGRES_PASSWORD:?" "$COMPOSE" && grep -q "AUTH_SECRET_KEY:?" "$COMPOSE"; then
        check_pass "Mandatory secrets use :? (fail-fast if unset)"
    else
        check_fail "Mandatory secrets not using :? syntax"
    fi

    # Security opt no-new-privileges
    COUNT=$(grep -c "no-new-privileges:true" "$COMPOSE" || true)
    if [ "$COUNT" -ge 2 ]; then
        check_pass "security_opt no-new-privileges on >=2 services ($COUNT)"
    else
        check_fail "no-new-privileges missing (found $COUNT, expected >=2)"
    fi

    # Networks
    if grep -q "networks:" "$COMPOSE" && grep -q "creator-os-net" "$COMPOSE"; then
        check_pass "Explicit bridge network creator-os-net"
    else
        check_warn "No explicit network (uses default)"
    fi

    # Resource limits
    if grep -q "deploy:" "$COMPOSE" && grep -q "limits:" "$COMPOSE"; then
        check_pass "Resource limits defined (cpus, memory)"
    else
        check_warn "No resource limits defined"
    fi

    # Logging with rotation
    if grep -q "logging:" "$COMPOSE" && grep -q "max-size" "$COMPOSE"; then
        check_pass "Log rotation configured (json-file max-size)"
    else
        check_warn "Log rotation not configured in compose"
    fi

    # ENV file
    if grep -q "env_file:" "$COMPOSE"; then
        check_pass "env_file used for backend"
    else
        check_warn "env_file not used"
    fi

    # TRUST_PROXY_HEADERS
    if grep -q 'TRUST_PROXY_HEADERS: "true"' "$COMPOSE"; then
        check_pass "TRUST_PROXY_HEADERS=true (correct behind frontend)"
    else
        check_fail "TRUST_PROXY_HEADERS not true"
    fi

    # Migrate is one-shot
    if grep -q 'profiles: \["tools"\]' "$COMPOSE" && grep -A5 "migrate:" "$COMPOSE" | grep -q 'restart: "no"'; then
        check_pass "migrate service is one-shot (tools profile, restart no)"
    else
        # More flexible check
        if grep -q "tools" "$COMPOSE" && grep -q 'restart: "no"' "$COMPOSE"; then
            check_pass "migrate service is one-shot"
        else
            check_fail "migrate not one-shot"
        fi
    fi

    # Backend does not run migrations
    BACKEND_BLOCK=$(sed -n '/^  backend:/,/^  frontend:/p' "$COMPOSE")
    if echo "$BACKEND_BLOCK" | grep -q "alembic"; then
        check_fail "backend service runs migrations (race condition with replicas)"
    else
        check_pass "backend does not run migrations (correct)"
    fi
fi

echo ""
echo "--- Environment Variables ---"
ENV_EXAMPLE="$ROOT/.env.production.example"
if [ ! -f "$ENV_EXAMPLE" ]; then
    check_fail ".env.production.example missing"
else
    for var in "POSTGRES_PASSWORD" "AUTH_SECRET_KEY" "CORS_ORIGINS" "ALLOWED_HOSTS" "POSTGRES_USER" "POSTGRES_DB" "HTTP_PORT"; do
        if grep -q "^#*.*$var=" "$ENV_EXAMPLE"; then
            check_pass "$var documented in .env.production.example"
        else
            check_fail "$var missing from .env.production.example"
        fi
    done

    # Check that compose ${VAR} are all documented
    echo "Checking compose env var coverage..."
    REFERENCED=$(grep -oE '\$\{[A-Z_][A-Z0-9_]*' "$COMPOSE" | sed 's/${//' | sort -u)
    for var in $REFERENCED; do
        if [ "$var" = "HTTP_PORT" ]; then
            continue
        fi
        if grep -q "^#*.*$var=" "$ENV_EXAMPLE"; then
            :
        else
            check_fail "Compose uses \${$var} but not documented in .env.production.example"
        fi
    done
fi

echo ""
echo "--- Nginx Config ---"
NGINX_CONF="$ROOT/frontend/nginx.conf"
if [ ! -f "$NGINX_CONF" ]; then
    check_fail "frontend/nginx.conf missing"
else
    if grep -q "proxy_pass http://backend:8000" "$NGINX_CONF"; then
        check_pass "nginx proxies to backend:8000 (correct service name and port)"
    else
        check_fail "nginx proxy_pass incorrect"
    fi

    if grep -q "proxy_buffering off" "$NGINX_CONF"; then
        check_pass "nginx proxy_buffering off (required for SSE)"
    else
        check_fail "nginx proxy_buffering off missing (breaks SSE)"
    fi

    if grep -q "proxy_read_timeout 3600s" "$NGINX_CONF"; then
        check_pass "nginx proxy_read_timeout 3600s (SSE)"
    else
        check_fail "proxy_read_timeout missing"
    fi

    if grep -q "client_max_body_size" "$NGINX_CONF"; then
        check_pass "client_max_body_size set"
    else
        check_fail "client_max_body_size missing"
    fi
fi

echo ""
echo "=== Summary ==="
echo -e "Passed: ${GREEN}$PASS${NC}"
echo -e "Failed: ${RED}$FAIL${NC}"
echo -e "Warnings: ${YELLOW}$WARN${NC}"

if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}Docker validation FAILED${NC}"
    exit 1
else
    echo -e "${GREEN}Docker validation PASSED${NC}"
    exit 0
fi
