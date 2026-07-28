#!/usr/bin/env bash
# M8: Backup script for Creator OS
# Supports both Docker and source-based deployments
#
# Usage:
#   ./scripts/backup.sh [backup-dir]
#   Default backup-dir: ./backups/<timestamp>

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BACKUP_DIR="${1:-$ROOT/backups/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$BACKUP_DIR"

echo "=== Creator OS Backup ==="
echo "Backup dir: $BACKUP_DIR"
echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# Check if docker-compose is available and stack is running
if command -v docker >/dev/null 2>&1 && docker compose ps 2>&1 | grep -q "creator-os"; then
    echo "Docker stack detected - using docker-compose backup"
    echo ""
    echo "--- Database backup (pg_dump) ---"
    if docker compose ps db 2>&1 | grep -q "running\|healthy\|Up"; then
        docker compose exec -T db pg_dump -U "${POSTGRES_USER:-creator}" "${POSTGRES_DB:-creator_os}" | gzip > "$BACKUP_DIR/database.sql.gz"
        echo "✓ Database dumped: $BACKUP_DIR/database.sql.gz ($(du -h "$BACKUP_DIR/database.sql.gz" | cut -f1))"
    else
        echo "⚠ Database container not running - skipping pg_dump"
    fi

    echo ""
    echo "--- Media backup (tar) ---"
    if docker volume ls | grep -q "media_data"; then
        docker run --rm -v creator-os_media_data:/data -v "$BACKUP_DIR:/backup" alpine tar czf /backup/media.tar.gz -C /data .
        echo "✓ Media archived: $BACKUP_DIR/media.tar.gz ($(du -h "$BACKUP_DIR/media.tar.gz" | cut -f1))"
    else
        echo "⚠ media_data volume not found - checking for local media_storage"
        if [ -d "./backend/media_storage" ] || [ -d "./media_storage" ]; then
            tar czf "$BACKUP_DIR/media.tar.gz" -C backend media_storage 2>/dev/null || tar czf "$BACKUP_DIR/media.tar.gz" -C . media_storage
            echo "✓ Media archived from filesystem: $BACKUP_DIR/media.tar.gz"
        else
            echo "⚠ No media found"
        fi
    fi
else
    echo "Source-based deployment detected (no Docker stack running)"
    echo ""
    echo "--- Database backup (SQLite or file copy) ---"
    # Try to detect DATABASE_URL
    DB_URL=$(grep DATABASE_URL .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "")
    if [[ "$DB_URL" == sqlite* ]]; then
        DB_PATH=$(echo "$DB_URL" | sed 's/sqlite:\/\/\///')
        if [ -f "backend/$DB_PATH" ]; then
            DB_PATH="backend/$DB_PATH"
        elif [ -f "$DB_PATH" ]; then
            :
        else
            # Default location
            DB_PATH="backend/creator_os.db"
        fi
        if [ -f "$DB_PATH" ]; then
            cp "$DB_PATH" "$BACKUP_DIR/database.db"
            gzip "$BACKUP_DIR/database.db"
            echo "✓ SQLite DB backed up: $BACKUP_DIR/database.db.gz"
        else
            echo "⚠ SQLite DB not found at $DB_PATH"
        fi
    else
        # M9-F3: this branch used to print a warning and exit 0 when pg_dump
        # was missing or failed, producing a "successful" backup directory with
        # no database in it. The failure only surfaced during a restore, which
        # is the worst possible moment. A backup that cannot capture the
        # database is now a hard error.
        echo "PostgreSQL deployment detected - using pg_dump"

        # Prefer the real DATABASE_URL: it already carries user, password, host
        # and port. Reconstructing it from POSTGRES_* dropped the host/port and
        # silently dumped whatever local cluster happened to answer.
        PG_URL="$DB_URL"
        if [ -z "$PG_URL" ]; then
            PG_URL="${DATABASE_URL:-}"
        fi
        # SQLAlchemy driver suffixes are not understood by libpq.
        PG_URL="${PG_URL/postgresql+psycopg:\/\//postgresql://}"
        PG_URL="${PG_URL/postgresql+psycopg2:\/\//postgresql://}"
        PG_URL="${PG_URL/postgres+psycopg:\/\//postgresql://}"

        PG_DUMP_BIN="${PG_DUMP:-}"
        if [ -z "$PG_DUMP_BIN" ]; then
            if command -v pg_dump >/dev/null 2>&1; then
                PG_DUMP_BIN="$(command -v pg_dump)"
            else
                # A venv install of pgserver ships a real pg_dump; prefer it
                # over failing when the host has no PostgreSQL client package.
                CANDIDATE=$(ls -d "$ROOT"/backend/.venv/lib/python*/site-packages/pgserver/pginstall/bin/pg_dump 2>/dev/null | head -1 || true)
                [ -n "$CANDIDATE" ] && PG_DUMP_BIN="$CANDIDATE"
            fi
        fi

        if [ -z "$PG_DUMP_BIN" ]; then
            echo "❌ pg_dump not found and DATABASE_URL is PostgreSQL."
            echo "   A backup without the database is worse than no backup: it"
            echo "   looks like protection it does not provide."
            echo "   Install the PostgreSQL client tools, or set PG_DUMP=/path/to/pg_dump."
            echo "   For a containerised stack, run this script on a host where"
            echo "   'docker compose ps' can see the stack."
            exit 1
        fi

        if [ -z "$PG_URL" ]; then
            echo "❌ No DATABASE_URL found in .env or the environment."
            echo "   Cannot determine which database to back up."
            exit 1
        fi

        echo "Using pg_dump: $PG_DUMP_BIN"
        if ! "$PG_DUMP_BIN" "$PG_URL" | gzip > "$BACKUP_DIR/database.sql.gz"; then
            echo "❌ pg_dump failed - no database backup was produced."
            rm -f "$BACKUP_DIR/database.sql.gz"
            exit 1
        fi
        # A dump that is empty or unreadable must not pass as a backup.
        if ! gunzip -t "$BACKUP_DIR/database.sql.gz" 2>/dev/null; then
            echo "❌ Backup archive is corrupt (gunzip -t failed)."
            exit 1
        fi
        DUMP_BYTES=$(stat -c%s "$BACKUP_DIR/database.sql.gz")
        if [ "$DUMP_BYTES" -lt 100 ]; then
            echo "❌ Backup archive is suspiciously small (${DUMP_BYTES} bytes)."
            exit 1
        fi
        echo "✓ Database dumped: $BACKUP_DIR/database.sql.gz ($(du -h "$BACKUP_DIR/database.sql.gz" | cut -f1))"
    fi

    echo ""
    echo "--- Media backup ---"
    # M9-F3: MEDIA_ROOT is configurable and defaults to something other than
    # backend/media_storage in every production deployment. The old code only
    # ever looked at the two hard-coded paths, so a configured deployment
    # silently archived an empty directory.
    MEDIA_DIR=$(grep -E '^MEDIA_ROOT=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "")
    [ -z "$MEDIA_DIR" ] && MEDIA_DIR="${MEDIA_ROOT:-}"
    if [ -n "$MEDIA_DIR" ] && [ -d "$MEDIA_DIR" ]; then
        tar czf "$BACKUP_DIR/media.tar.gz" -C "$(dirname "$MEDIA_DIR")" "$(basename "$MEDIA_DIR")"
        echo "✓ Media from MEDIA_ROOT=$MEDIA_DIR: $BACKUP_DIR/media.tar.gz ($(du -h "$BACKUP_DIR/media.tar.gz" | cut -f1))"
    elif [ -d "backend/media_storage" ]; then
        tar czf "$BACKUP_DIR/media.tar.gz" -C backend media_storage
        echo "✓ Media: $BACKUP_DIR/media.tar.gz"
    elif [ -d "media_storage" ]; then
        tar czf "$BACKUP_DIR/media.tar.gz" -C . media_storage
        echo "✓ Media: $BACKUP_DIR/media.tar.gz"
    else
        echo "⚠ No media directory found (MEDIA_ROOT='$MEDIA_DIR')"
    fi
fi

echo ""
echo "--- Config backup (without secrets) ---"
# Backup env template and sanitized config
if [ -f .env ]; then
    # Redact secrets
    sed -E 's/(PASSWORD|SECRET_KEY|API_KEY).*=.*/\1=***REDACTED***/' .env > "$BACKUP_DIR/env.sanitized"
    echo "✓ Sanitized env: $BACKUP_DIR/env.sanitized"
    echo "⚠ Full .env NOT backed up automatically - back up securely separately!"
    echo "  Without AUTH_SECRET_KEY and POSTGRES_PASSWORD, restore is incomplete"
fi

echo ""
echo "--- Manifest ---"
cat > "$BACKUP_DIR/manifest.txt" <<EOF
Creator OS Backup Manifest
Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)
Hostname: $(hostname)
Git commit: $(git rev-parse HEAD 2>/dev/null || echo "unknown")
Version: $(grep -E '^__version__' backend/app/version.py 2>/dev/null | cut -d'"' -f2 || echo "unknown")
Files:
$(cd "$BACKUP_DIR" && ls -lh | tail -n +2 | grep -v ' manifest.txt$')
SHA256:
$(cd "$BACKUP_DIR" && sha256sum ./* 2>/dev/null | grep -v 'manifest.txt')
EOF
cat "$BACKUP_DIR/manifest.txt"

# M9-F3: the restore path depends on the database archive existing. Fail here
# rather than let an operator discover it during an incident.
if [ ! -f "$BACKUP_DIR/database.sql.gz" ] && [ ! -f "$BACKUP_DIR/database.db.gz" ]; then
    echo ""
    echo "❌ No database archive in $BACKUP_DIR - this backup is NOT restorable."
    exit 1
fi

echo ""
echo "=== Backup completed: $BACKUP_DIR ==="
echo "Next steps:"
echo "  - Verify backup integrity: gunzip -t $BACKUP_DIR/*.gz"
echo "  - Test restore on staging: ./scripts/restore.sh $BACKUP_DIR"
echo "  - Store securely (contains sensitive data if you manually added .env)"
echo "  - Schedule regularly via cron"
ls -lh "$BACKUP_DIR"
