# Creator OS backend image (M5).
#
# Multi-stage so the runtime layer carries no build toolchain. The final image
# runs as an unprivileged user, has a healthcheck, and treats the application
# directory as read-only data (writable state lives in mounted volumes).

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

# ffmpeg/ffprobe are optional at runtime: the media pipeline degrades
# gracefully without them, but transcoding and probing need them present.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

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

USER creator

EXPOSE 8000

# Uses the liveness probe: cheap, and it does not touch the database.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health/live" || exit 1

# Migrations are deliberately NOT run here. An image that migrates on start
# races with itself when scaled to more than one replica; run
# `alembic upgrade head` as a separate release step (see docs/DEPLOYMENT.md).
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$HOST\" --port \"$PORT\" --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'"]
