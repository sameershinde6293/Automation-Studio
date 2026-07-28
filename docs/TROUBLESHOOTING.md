# Troubleshooting

Creator OS v1.1.1

Symptom-first. Every entry marked **(verified in M7)** was actually reproduced
and resolved during the release audit, not written from theory.

---

## Diagnose first

```bash
curl -s localhost:8000/health/ready | python3 -m json.tool
```

`/health/ready` names the failing subsystem — `database`, `scheduler`,
`execution_workers` or `configuration` — and returns `503` when degraded.
`/health/live` answers even when the database is down, which is how you tell a
crashed process from a broken dependency.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  localhost:8000/api/system/config/validation      # every config finding
curl -s localhost:8000/api/system/errors           # recent aggregated errors
```

---

## Startup

### Backend exits immediately: "Refusing to start: unsafe production configuration"

Working as intended — startup validation rejected the config. Each finding
names the key and its remedy. Common causes: `AUTH_ENABLED=false`,
wildcard `CORS_ORIGINS`, or a missing `AUTH_SECRET_KEY` in production.

`ALLOW_INSECURE_PRODUCTION=true` overrides it. It is logged loudly and is not a
supported configuration.

### Every endpoint 404s

You started the wrong app. `backend/main.py` is a V1.0 stub with no routers.

```bash
./.venv/bin/uvicorn app.main:app     # correct
```

### `sqlite3.OperationalError: no such table: workflows`

Migrations were never applied, **or** the process is reading a different
database than you migrated.

```bash
cd backend && ./.venv/bin/alembic upgrade head
./.venv/bin/alembic current           # should print a revision, not nothing
```

If a stray `backend/creator_os.db` appeared while you expected PostgreSQL, your
`.env` is not being read — see the next entry.

### `.env` is ignored; the app boots with defaults **(verified in M7)**

A pre-rc1 build. `.env` was resolved relative to the working directory, so a
file at the repository root was silently skipped when starting from `backend/`,
and the process fell back to **every default** — `development`, authentication
**off**, Swagger **exposed**, SQLite instead of PostgreSQL. Nothing warned,
because the production gate only engages when it believes it is in production.

Fixed in v1.1.0-rc1 (M7-F1). Confirm what is actually loaded:

```bash
cd backend && ./.venv/bin/python -c \
  "from app.infrastructure.config.settings import settings; \
   print(settings.ENVIRONMENT, settings.AUTH_ENABLED, settings.DATABASE_URL)"
```

On an older build, or to be explicit:

```bash
CREATOR_OS_ENV_FILE=/absolute/path/to/.env ./.venv/bin/uvicorn app.main:app
```

Search order, lowest precedence first: `<repo>/.env` → `<repo>/backend/.env` →
`$PWD/.env` → real environment variables.

### `Settings(_env_file=...)` returns defaults **(verified in M7)**

Pre-rc1 defect (M7-F2): the custom settings sources discarded the per-instance
`_env_file` override. Affects code and tests that load an alternate config; the
running server was unaffected. Fixed in v1.1.0-rc1.

### Backend will not start after editing `.env` (`SettingsError`)

Pre-M6 build that could not parse comma-separated lists. Both forms work now:

```bash
CORS_ORIGINS=https://a.example.com,https://b.example.com
CORS_ORIGINS=["https://a.example.com","https://b.example.com"]
```

---

## Installation

### `npm install` fails downloading Electron

TLS-inspecting proxy blocking the binary download.

```bash
ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm install
```

Sufficient for `npm run dev`, `npm test`, `npm run typecheck` and
`npx vite build`. Only launching the desktop shell needs the binary.

### `pip install` tries to compile something

It should not — every dependency ships a prebuilt wheel. Upgrade pip first:

```bash
./.venv/bin/pip install --upgrade pip
```

### Port already in use

```bash
./.venv/bin/uvicorn app.main:app --port 8001
npm run dev -- --port 5174        # then set CORS_ORIGINS to match
```

---

## Workflows

### A template renders empty **(verified in M7)**

Variables seeded by the `start` node are reached **through the node**:

```
{{ Start.variables.my_var }}    correct
{{ my_var }}                    empty — a bare name is not a run variable
{{ NodeName.field }}            an upstream node's output field
{{ item }}                      the current element inside a loop
```

This bit the M7 examples: `{{ Start.topic }}` silently produced `""`, and the
HTTP node then failed with "requires a non-empty 'url'". An empty render is not
an error, so nothing warns — inspect the execution to see what a node received:

```bash
curl -s localhost:8000/api/workflows/executions/<id> | python3 -m json.tool
```

`state.variables` and each node's `output_data` show exactly what resolved.

### `CERTIFICATE_VERIFY_FAILED` from HTTP/webhook nodes **(verified in M7)**

Corporate or sandbox TLS interception. The backend's `httpx` client validates
against the `certifi` bundle, which does not contain your proxy's CA — so
`curl` succeeds while the node fails.

```bash
SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt ./.venv/bin/uvicorn app.main:app
```

Confirm it is a trust-store issue, not connectivity:

```bash
./.venv/bin/python -c "
import httpx, ssl
ctx = ssl.create_default_context(cafile='/etc/ssl/certs/ca-certificates.crt')
print(httpx.get('https://api.github.com', verify=ctx).status_code)"
```

`200` means the network is fine and only the trust store was wrong.

### HTTP node refuses a URL: SSRF guard

Private, loopback, link-local and metadata addresses are blocked deliberately.

```bash
HTTP_EXECUTOR_ALLOWED_HOSTS=internal-api.corp     # preferred: allowlist
HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS=true         # blunt; understand the risk
```

### Executions stay `QUEUED`

The worker pool did not start — check `execution_workers` in `/health/ready`.
Queued runs do **not** survive a restart (`KNOWN_ISSUES.md` #1): rows persist as
`QUEUED` but are never re-claimed. Re-trigger them.

### Live updates never arrive

Something between client and backend is buffering the SSE stream. Every proxy
hop needs:

```nginx
proxy_buffering off;
proxy_read_timeout 3600s;
```

Poll `GET /api/workflows/executions/{id}` to confirm the run itself is fine.

### Python or JavaScript node fails with "executor disabled"

Both are off by default. **Read [SECURITY.md](SECURITY.md) §5 first** — the
Python sandbox is defence in depth, not a security boundary, and the JavaScript
node is not sandboxed at all.

```bash
ALLOW_PYTHON_EXECUTOR=true
```

### Media node reports FFmpeg missing

Install `ffmpeg`/`ffprobe`, or point `FFMPEG_BINARY` and `FFPROBE_BINARY` at
them. Image handling works without FFmpeg via Pillow.

### Image / TTS / STT node fails with a `provider` error

No provider ships by default (`KNOWN_ISSUES.md` #16). Register one via
`ai_orchestrator.register_*_provider`.

---

## Authentication

### Everything returns 401

Auth is enabled and no credential was sent.

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"…"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/workflows/
```

Access tokens expire in 15 minutes by default; refresh, or use an API key
(`X-API-Key`) for automation.

### No admin account exists

Self-registration is off by default. Bootstrap one:

```bash
AUTH_BOOTSTRAP_USERNAME=admin
AUTH_BOOTSTRAP_PASSWORD=<strong temporary passphrase>
```

Created **only** when the user table is empty. Log in, change the password,
clear both variables, restart. Startup validation warns while they are present.

### Locked out after failed logins

`AUTH_MAX_FAILED_LOGINS` (5) triggers a lockout for `AUTH_LOCKOUT_SECONDS`
(900). Wait it out, or clear `failed_login_count` in the `users` table.

### 400 on every request behind a proxy

`ALLOWED_HOSTS` does not include the `Host` header your proxy sends. Verified in
M7: a mismatched host correctly returns `400`.

---

## Performance

### 503 `database_unavailable` under load

The connection pool is exhausted. This is load shedding working, not a crash —
`/health/live` keeps answering.

```bash
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=60      # capacity 80 served 100 concurrent clients with 0 errors
```

Keep `(DB_POOL_SIZE + DB_MAX_OVERFLOW) × replicas` below PostgreSQL
`max_connections`. Measured curve: [DEPLOYMENT.md](DEPLOYMENT.md) §9.

### 429s under normal load

Raise `RATE_LIMIT_REQUESTS`, or check `TRUST_PROXY_HEADERS` — if false behind a
proxy, every client shares one bucket keyed by the proxy's address. Note the
limiter is per-process, so N workers give N× the configured limit
(`KNOWN_ISSUES.md` #2).

### Slow with SQLite

Expected. SQLite is single-writer; move to PostgreSQL for concurrency.

---

## Database and migrations

### `DuplicateObject: type "executionstatus" already exists`

A pre-M6 build left PostgreSQL enum types behind on downgrade. Fixed in M6 and
re-verified in M7 over a full `upgrade → downgrade → upgrade` cycle plus a
complete downgrade-to-base (**0 orphaned enums**). To clean a database migrated
by an old build:

```sql
SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = 'public' AND t.typtype = 'e';
DROP TYPE IF EXISTS executionstatus, loglevel;
```

### Multiple migration heads

```bash
./.venv/bin/alembic heads       # must print exactly one
./.venv/bin/alembic merge -m "merge heads" <rev1> <rev2>
```

### Restore verification

Verified in M7 — `pg_dump | gzip` → `DELETE FROM workflows` → `gunzip | psql`
recovered every row.

```bash
pg_dump -U creator creator_os | gzip > backup-$(date +%F).sql.gz
gunzip -c backup-2026-07-27.sql.gz | psql -U creator creator_os
```

Back up `.env` separately and securely: without `AUTH_SECRET_KEY` every session
is invalidated, and the database is useless without its password.

---

## Docker

> **The Docker path has never been executed** — no container runtime was
> available in M5, M6 or M7. The guidance below is derived from the asset
> definitions and their static validation, not from a running stack. See
> [M7_RELEASE_AUDIT.md](M7_RELEASE_AUDIT.md) §6.

| Symptom | Likely cause |
| --- | --- |
| `POSTGRES_PASSWORD must be set` | Compose `:?` guard firing — populate `.env` |
| Backend unhealthy, db healthy | Migrations not run: `docker compose --profile tools run --rm migrate` |
| Frontend 502 | Backend not healthy yet; nginx proxies to `backend:8000` |
| Data lost after `docker compose down` | Use `down` without `-v`; `-v` deletes named volumes |

---

## Still stuck

1. `/health/ready` — which subsystem is degraded?
2. `/api/system/config/validation` — any configuration findings?
3. Logs with `LOG_LEVEL=DEBUG` (secrets are redacted automatically).
4. [KNOWN_ISSUES.md](KNOWN_ISSUES.md) — it may be a documented limitation.
5. Open an issue with the `/health/ready` body, the relevant log lines, and your
   redacted `.env`.
