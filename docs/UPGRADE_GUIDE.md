# Upgrade Guide

Creator OS v1.1.0

The order below is not arbitrary: **schema first, then code.** A new binary
against an old schema fails on the first query; an old binary against a new
schema usually keeps working, which is what makes rollback survivable.

---

## Before you start

```bash
# 1. Database
pg_dump -U creator creator_os | gzip > backup-$(date +%F).sql.gz

# 2. Media
tar czf media-$(date +%F).tar.gz -C "$MEDIA_ROOT" .

# 3. Configuration and the current revision — both are needed to roll back
cp .env "env-backup-$(date +%F)"
cd backend && ./.venv/bin/alembic current | tee "../revision-$(date +%F).txt"
```

Back up `.env` somewhere secure. Without `AUTH_SECRET_KEY` every session is
invalidated, and the dump is useless without the database password.

Restore was rehearsed in M7 (`pg_dump` → destructive `DELETE` → restore
recovered every row), so these commands are known to work — but rehearse them on
*your* infrastructure before you need them.

---

## Source deployment

```bash
# 1. Stop the service (SIGTERM — the app drains and shuts down cleanly)
systemctl stop creator-os

# 2. Fetch
git fetch --tags
git checkout v1.1.0

# 3. Dependencies
cd backend && ./.venv/bin/pip install -r requirements.txt

# 4. Schema
./.venv/bin/alembic upgrade head

# 5. Frontend
cd ../frontend && ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm ci && npx tsc && npx vite build

# 6. Start
systemctl start creator-os
```

### Verify

```bash
curl -fsS localhost:8000/health/ready | python3 -m json.tool   # all checks ok
curl -s -H "Authorization: Bearer $TOKEN" \
  localhost:8000/api/system/config/validation                  # 0 errors
python scripts/verify_examples.py                              # 4/4 passed
```

Then confirm the version:

```bash
curl -s localhost:8000/health/ready | grep -o '"version":"[^"]*"'
```

---

## Docker deployment

```bash
git pull
docker compose build
docker compose --profile tools run --rm migrate     # 1. schema
docker compose up -d                                # 2. code
curl -fsS http://localhost:8080/health/ready
```

Never migrate from the application container: with more than one replica they
race. The `migrate` service exists as a one-shot release step.

> **Unverified.** The Docker path has never been executed — see
> [M7_RELEASE_AUDIT.md](M7_RELEASE_AUDIT.md) §6. The commands are correct per
> the compose definition and statically validated, but they have not been run.

---

## Rollback

**Verified in M7** against PostgreSQL 16.2: `downgrade -1` → `upgrade head`
round-tripped cleanly, and a full `downgrade base` → `upgrade head` cycle left
**zero orphaned enum types** and restored all 19 tables.

### Code only (schema unchanged)

The common case, and the safe one.

```bash
systemctl stop creator-os
git checkout <previous-tag>
cd backend && ./.venv/bin/pip install -r requirements.txt
systemctl start creator-os
```

### Code and schema

```bash
systemctl stop creator-os

cd backend
./.venv/bin/alembic downgrade -1        # or: downgrade <revision-from-your-notes>

cd .. && git checkout <previous-tag>
cd backend && ./.venv/bin/pip install -r requirements.txt
systemctl start creator-os
```

Docker equivalent:

```bash
docker compose --profile tools run --rm migrate alembic downgrade -1
git checkout <previous-tag>
docker compose build && docker compose up -d
```

### Last resort: restore from backup

```bash
systemctl stop creator-os
dropdb creator_os && createdb creator_os
gunzip -c backup-2026-07-27.sql.gz | psql -U creator creator_os
git checkout <previous-tag>
cp env-backup-2026-07-27 .env
systemctl start creator-os
```

You lose everything written since the dump. That is the trade for certainty.

---

## Version-specific notes

### → v1.1.0 (this release, M10 GA)

**No schema change.** The head revision is still `d5f3a7c81b64`; you can upgrade
and roll back freely.

**Read this if you deploy from source.** Two configuration defects were fixed,
and the first one changes observable behaviour:

- **M7-F1** — a `.env` at the repository root was previously **ignored** when
  starting the server from `backend/`, and the process silently fell back to
  every default: `development`, authentication **off**, Swagger **exposed**,
  SQLite instead of PostgreSQL.

  **If your deployment was affected, it has been running unauthenticated.**
  After upgrading, that `.env` is honoured — so settings that were being ignored
  now take effect. Check what will actually load *before* restarting:

  ```bash
  cd backend && ./.venv/bin/python -c \
    "from app.infrastructure.config.settings import settings; \
     print(settings.ENVIRONMENT, settings.AUTH_ENABLED, settings.DATABASE_URL)"
  ```

  If that prints `production True postgresql…` where the running process was
  using development defaults, expect authentication to start being enforced and
  the database to switch. Confirm `DATABASE_URL` points at the database you have
  actually migrated.

- **M7-F2** — `Settings(_env_file=...)` silently ignored the file. Affects code
  and tests that load an alternate configuration; the running server was not
  affected.

Precedence is unchanged and backwards compatible: `<repo>/.env` →
`<repo>/backend/.env` → `$PWD/.env` → real environment variables. A working
directory `.env` still wins, so existing setups behave exactly as before.

### → v1.1.0 (M6)

Comma-separated list settings (`CORS_ORIGINS=a,b`) parse correctly; previously
they crashed the process at import. PostgreSQL `downgrade` no longer leaves enum
types behind — if you rolled back on a pre-M6 build, check for orphans:

```sql
SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = 'public' AND t.typtype = 'e';
```

### → v1.1.0 (M5)

Adds identity tables and the previously missing `audit_events` migration.
Authentication is **off** by default for backwards compatibility, and
**mandatory** in production. Set `AUTH_ENABLED=true`, supply `AUTH_SECRET_KEY`,
and bootstrap the first admin — see [DEPLOYMENT.md](DEPLOYMENT.md) §3.

---

## Zero-downtime upgrades

Not currently supported. Creator OS runs as a single backend process, so an
upgrade means a restart. In-flight executions do not survive it, and queued runs
are **not** re-claimed (`KNOWN_ISSUES.md` #1).

Before upgrading:

```bash
curl -s localhost:8000/api/executions?status=RUNNING   # drain first
curl -s localhost:8000/metrics | grep queue_depth      # should be 0
```

---

## Post-upgrade checklist

- [ ] `/health/ready` returns `200` with all checks `ok`
- [ ] `/api/system/config/validation` reports zero errors
- [ ] `alembic current` matches the expected head
- [ ] Authentication behaves as intended (especially upgrading to rc1)
- [ ] A known workflow executes: `python scripts/verify_examples.py`
- [ ] Metrics are being scraped and logs collected
- [ ] The backup you took is still available, and you know how to restore it
