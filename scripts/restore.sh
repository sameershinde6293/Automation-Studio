#!/usr/bin/env bash
# M8: Restore script for Creator OS
# Supports both Docker and source-based deployments
#
# Usage:
#   ./scripts/restore.sh <backup-dir>
#
# WARNING: This overwrites current data! Use on staging first.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <backup-dir>"
    echo "Example: $0 ./backups/20260727-144500"
    exit 1
fi

BACKUP_DIR="$1"

if [ ! -d "$BACKUP_DIR" ]; then
    echo "❌ Backup dir not found: $BACKUP_DIR"
    exit 1
fi

echo "=== Creator OS Restore ==="
echo "Backup dir: $BACKUP_DIR"
echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Manifest:"
cat "$BACKUP_DIR/manifest.txt" 2>/dev/null || echo "(no manifest)"
echo ""

read -p "⚠️  This will OVERWRITE current database and media. Continue? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted"
    exit 1
fi

echo ""

# Check deployment type
if command -v docker >/dev/null 2>&1 && docker compose ps 2>&1 | grep -q "db\|backend"; then
    echo "Docker stack detected"
    echo ""
    echo "--- Restoring database ---"
    if [ -f "$BACKUP_DIR/database.sql.gz" ]; then
        echo "Restoring PostgreSQL from $BACKUP_DIR/database.sql.gz"
        # Drop and recreate? For safety, we just restore over existing
        # In production you might want to: docker compose down, volume rm, up, then restore
        gunzip -c "$BACKUP_DIR/database.sql.gz" | docker compose exec -T db psql -U "${POSTGRES_USER:-creator}" "${POSTGRES_DB:-creator_os}"
        echo "✓ Database restored"
    else
        echo "⚠ No database.sql.gz found"
    fi

    echo ""
    echo "--- Restoring media ---"
    if [ -f "$BACKUP_DIR/media.tar.gz" ]; then
        docker run --rm -v creator-os_media_data:/data -v "$BACKUP_DIR:/backup" alpine sh -c "rm -rf /data/* && tar xzf /backup/media.tar.gz -C /data"
        echo "✓ Media restored"
    else
        echo "⚠ No media.tar.gz found"
    fi
else
    echo "Source-based deployment detected"
    echo ""
    echo "--- Restoring database ---"
    DB_URL=$(grep DATABASE_URL .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "")

    if [ -f "$BACKUP_DIR/database.db.gz" ]; then
        echo "Restoring SQLite from $BACKUP_DIR/database.db.gz"
        gunzip -c "$BACKUP_DIR/database.db.gz" > /tmp/restore.db
        # Detect target path
        TARGET="backend/creator_os.db"
        if [[ "$DB_URL" == sqlite* ]]; then
            DB_PATH=$(echo "$DB_URL" | sed 's/sqlite:\/\/\///')
            if [[ "$DB_PATH" == ./* ]] || [[ "$DB_PATH" == /* ]]; then
                TARGET="$DB_PATH"
            else
                TARGET="backend/$DB_PATH"
            fi
        fi
        echo "Target: $TARGET"
        cp /tmp/restore.db "$TARGET"
        rm /tmp/restore.db
        echo "✓ Database restored"
    elif [ -f "$BACKUP_DIR/database.sql.gz" ]; then
        # M9-F3: this branch used to pipe into `psql "${DATABASE_URL:-}"`, which
        # is empty unless DATABASE_URL happens to be exported. psql then fell
        # back to the local unix socket and the operator saw "⚠ Manual restore
        # needed" (or, worse, a restore into the wrong database) while the
        # script still exited 0. Read the URL from .env, translate the
        # SQLAlchemy driver suffix, and treat any failure as fatal.
        echo "Restoring PostgreSQL from SQL dump"
        PG_URL="$DB_URL"
        [ -z "$PG_URL" ] && PG_URL="${DATABASE_URL:-}"
        PG_URL="${PG_URL/postgresql+psycopg:\/\//postgresql://}"
        PG_URL="${PG_URL/postgresql+psycopg2:\/\//postgresql://}"
        PG_URL="${PG_URL/postgres+psycopg:\/\//postgresql://}"

        PSQL_BIN="${PSQL:-}"
        if [ -z "$PSQL_BIN" ]; then
            if command -v psql >/dev/null 2>&1; then
                PSQL_BIN="$(command -v psql)"
            else
                CANDIDATE=$(ls -d "$ROOT"/backend/.venv/lib/python*/site-packages/pgserver/pginstall/bin/psql 2>/dev/null | head -1 || true)
                [ -n "$CANDIDATE" ] && PSQL_BIN="$CANDIDATE"
            fi
        fi
        if [ -z "$PSQL_BIN" ]; then
            echo "❌ psql not found. Install the PostgreSQL client tools or set PSQL=/path/to/psql."
            exit 1
        fi
        if [ -z "$PG_URL" ]; then
            echo "❌ No DATABASE_URL in .env or the environment; refusing to guess the target database."
            exit 1
        fi
        if ! gunzip -t "$BACKUP_DIR/database.sql.gz" 2>/dev/null; then
            echo "❌ Backup archive is corrupt (gunzip -t failed); aborting before touching the database."
            exit 1
        fi
        echo "Target: $(echo "$PG_URL" | sed -E 's#(//[^:]+):[^@]+@#\1:***@#')"
        # ON_ERROR_STOP makes a partial restore an error instead of a database
        # left half-populated with a zero exit status.
        if ! gunzip -c "$BACKUP_DIR/database.sql.gz" \
             | "$PSQL_BIN" -v ON_ERROR_STOP=1 --quiet "$PG_URL" > /tmp/m9_restore.log 2>&1; then
            echo "❌ Restore failed. Last lines:"
            tail -20 /tmp/m9_restore.log
            exit 1
        fi
        echo "✓ Database restored"
    else
        echo "❌ No database backup found in $BACKUP_DIR - nothing to restore."
        exit 1
    fi

    echo ""
    echo "--- Restoring media ---"
    # M9-F3: mirror the backup side, which archives MEDIA_ROOT rather than
    # assuming backend/media_storage.
    MEDIA_DIR=$(grep -E '^MEDIA_ROOT=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" || echo "")
    [ -z "$MEDIA_DIR" ] && MEDIA_DIR="${MEDIA_ROOT:-}"
    if [ -f "$BACKUP_DIR/media.tar.gz" ]; then
        if [ -n "$MEDIA_DIR" ]; then
            mkdir -p "$(dirname "$MEDIA_DIR")"
            rm -rf "${MEDIA_DIR:?}"/*
            tar xzf "$BACKUP_DIR/media.tar.gz" -C "$(dirname "$MEDIA_DIR")"
            echo "✓ Media restored to MEDIA_ROOT=$MEDIA_DIR"
        else
            mkdir -p backend/media_storage
            rm -rf backend/media_storage/*
            tar xzf "$BACKUP_DIR/media.tar.gz" -C backend
            echo "✓ Media restored to backend/media_storage"
        fi
    else
        echo "⚠ No media.tar.gz found"
    fi
fi

echo ""
echo "--- Post-restore verification ---"
echo "Checking health if backend is running..."
curl -fsS http://localhost:8000/health 2>/dev/null | python3 -m json.tool || echo "Backend not running (start it to verify)"
curl -fsS http://localhost:8080/health 2>/dev/null | python3 -m json.tool || echo "Frontend/backend stack not running on 8080"

echo ""
echo "=== Restore completed ==="
echo "Next steps:"
echo "  - Run migrations if needed: alembic upgrade head or docker compose --profile tools run --rm migrate"
echo "  - Restart backend: docker compose up -d backend or systemctl restart creator-os"
echo "  - Verify: curl /health/ready and check logs"
