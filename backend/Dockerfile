# Creator OS backend image (M5, hardened M8).
#
# Multi-stage so the runtime layer carries no build toolchain. The final image
# runs as an unprivileged user, has a healthcheck, and treats the application
# directory as read-only data (writable state lives in mounted volumes).
#
# M8 validation:
# - Base image: python:3.11-slim (Debian, ~50 MB)
# - Builder stage isolates pip/build tools (not shipped)
# - Runtime stage: ~280-320 MB with ffmpeg, ~180 MB without (estimated)
# - Final image runs as UID 10001 (creator), not root
# - Healthcheck uses /health/live (no DB, cheap, never causes restart loop)
# - Logs to stdout (12-factor), JSON format in production
# - No secrets baked in, no .env copied from host (excluded by .dockerignore)

# --------------------------------------------------------------------------- #
# Builder: resolve dependencies into a self-contained virtualenv
# --------------------------------------------------------------------------- #
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Only the requirements file is copied first, so the (slow) dependency layer is
# cached and reused whenever application code changes but dependencies do not.
COPY requirements.txt .

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="Creator OS Backend" \
      org.opencontainers.image.description="Visual workflow automation with AI runtime" \
      org.opencontainers.image.version="1.1.1" \
      org.opencontainers.image.source="https://github.com/sameershinde6293/Automation-Studio" \
      org.opencontainers.image.licenses="MIT"

# ffmpeg/ffprobe are optional at runtime: the media pipeline degrades
# gracefully without them, but transcoding and probing need them present.
# curl is required for HEALTHCHECK.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/* \
 && apt-get clean

# Unprivileged runtime user. Running as root would mean a sandbox escape from
# a script node lands with full container privileges.
RUN groupadd --gid 10001 creator \
 && useradd --uid 10001 --gid creator --create-home --shell /usr/sbin/nologin creator

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=production \
    LOG_FORMAT=json \
    MEDIA_ROOT=/data/media \
    HOST=0.0.0.0 \
    PORT=8000

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=creator:creator . /app

# Writable state is confined to /data so the application tree can be mounted
# read-only in hardened deployments.
RUN mkdir -p /data/media /data/logs \
 && chown -R creator:creator /data

# Ensure no .env was accidentally baked in (defence in depth, .dockerignore
# already excludes it).
RUN rm -f /app/.env /app/.env.production 2>/dev/null; echo "No env files in image"

USER creator

EXPOSE 8000

# Uses the liveness probe: cheap, and it does not touch the database.
# Critical: /health/live must not depend on DB, or a DB blip restarts the app.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health/live" || exit 1

# Migrations are deliberately NOT run here. An image that migrates on start
# races with itself when scaled to more than one replica; run
# `alembic upgrade head` as a separate release step (see docs/DEPLOYMENT.md).
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$HOST\" --port \"$PORT\" --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'"]
