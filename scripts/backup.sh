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
        echo "⚠ DATABASE_URL is PostgreSQL or not set - using pg_dump if pg_dump available"
        if command -v pg_dump >/dev/null 2>&1; then
            # Try to get connection info from .env
            POSTGRES_USER=$(grep POSTGRES_USER .env 2>/dev/null | cut -d= -f2 || echo "creator")
            POSTGRES_DB=$(grep POSTGRES_DB .env 2>/dev/null | cut -d= -f2 || echo "creator_os")
            pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$BACKUP_DIR/database.sql.gz" || echo "⚠ pg_dump failed"
        else
            echo "⚠ pg_dump not available"
        fi
    fi

    echo ""
    echo "--- Media backup ---"
    if [ -d "backend/media_storage" ]; then
        tar czf "$BACKUP_DIR/media.tar.gz" -C backend media_storage
        echo "✓ Media: $BACKUP_DIR/media.tar.gz"
    elif [ -d "media_storage" ]; then
        tar czf "$BACKUP_DIR/media.tar.gz" -C . media_storage
        echo "✓ Media: $BACKUP_DIR/media.tar.gz"
    else
        echo "⚠ No media_storage directory found"
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
Version: $(cat backend/app/version.py 2>/dev/null | grep __version__ | cut -d'"' -f2 || echo "unknown")
Files:
$(ls -lh "$BACKUP_DIR")
EOF
cat "$BACKUP_DIR/manifest.txt"

echo ""
echo "=== Backup completed: $BACKUP_DIR ==="
echo "Next steps:"
echo "  - Verify backup integrity: gunzip -t $BACKUP_DIR/*.gz"
echo "  - Test restore on staging: ./scripts/restore.sh $BACKUP_DIR"
echo "  - Store securely (contains sensitive data if you manually added .env)"
echo "  - Schedule regularly via cron"
ls -lh "$BACKUP_DIR"
