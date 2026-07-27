# Deployment

Creator OS v1.1.0-rc1 · last updated 2026-07-27 (M7)

How to run Creator OS as a server. For the local desktop application see
`INSTALLATION_GUIDE.md`.

> **Status — updated in M7.** M7 re-executed this procedure from a **clean
> clone** against real PostgreSQL 16.2, and found a critical defect M6 had
> missed: a `.env` written at the repository root — as §2 below instructs — was
> **silently ignored** when the server was started from `backend/`. The process
> did not fail. It fell back to every default, booting in `development` with
> **authentication off** and **Swagger exposed**, while the migrated PostgreSQL
> database sat unused. Fixed (M7-F1); see `M7_RELEASE_AUDIT.md`.
>
> **Verified end to end in M7:** fresh-clone install, PostgreSQL migrations
> (upgrade, downgrade, full round trip to base with zero orphaned enum types),
> production startup with the complete security posture (`/docs` 404,
> unauthenticated API 401, host-header injection 400), bootstrap admin creation,
> JWT login, RBAC enforcement, `/health/live`, `/health/ready`, `/metrics`,
> configuration validation, JSON logging with **verified secret redaction**,
> graceful shutdown, restart with data intact, and `pg_dump` → destructive
> delete → restore recovering every row. The full backend suite passes against
> PostgreSQL with **zero skips** (1446 tests).
>
> **Still unverified:** the **Docker layer itself** — image build and
> `docker compose up`. No container runtime has been available in M5, M6 *or*
> M7. Every process the container would run has been validated outside it, and
> 23 static tests now check the asset definitions for internal consistency, but
> that is not the same as running the stack. **Treat the first containerised
> deployment as a validation exercise.**
>
> Full evidence: `M7_RELEASE_AUDIT.md` (current), `M6_VALIDATION_REPORT.md`.

---

## 0. Where `.env` goes

The file is discovered at the **repository root**, in `backend/`, or in the
process working directory — in that order, with later entries taking
precedence, and real environment variables outranking all of them. Set
`CREATOR_OS_ENV_FILE=/absolute/path` to override the search entirely.

Confirm what a deployment will actually load before starting it:

```bash
cd backend && ./.venv/bin/python -c \
  "from app.infrastructure.config.settings import settings; \
   print(settings.ENVIRONMENT, settings.AUTH_ENABLED, settings.DATABASE_URL)"
```

If that prints `development False sqlite://…` when you expected production,
stop: the configuration is not reaching the process.

---

## 1. Requirements

| Component | Minimum | Notes |
| --- | --- | --- |
| Docker Engine | 24+ | with the Compose v2 plugin |
| CPU / RAM | 2 vCPU / 2 GB | plus headroom per concurrent execution |
| Disk | 10 GB | mostly media assets |
| PostgreSQL | 14+ | provided by the stack; SQLite is not viable for a server |
| TLS termination | — | **required**; the stack does not terminate TLS itself |

---

## 2. Quick start

```bash
git clone https://github.com/sameershinde6293/Automation-Studio.git
cd Automation-Studio

cp .env.production.example .env
```

Edit `.env`. Two values are mandatory and have no defaults:

```bash
# 1. Signing secret — unique per deployment
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
# → AUTH_SECRET_KEY=...

# 2. Database password
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
# → POSTGRES_PASSWORD=...
```

Also set `CORS_ORIGINS` and `ALLOWED_HOSTS` to your real hostname.

```bash
# Apply migrations FIRST, as a separate release step (see §4)
docker compose --profile tools run --rm migrate

# Start the stack
docker compose up -d

# Verify
curl -fsS http://localhost:8080/health/ready
```

---

## 3. Creating the first administrator

The user table starts empty and self-registration is off, so bootstrap once:

```bash
# In .env
AUTH_BOOTSTRAP_USERNAME=admin
AUTH_BOOTSTRAP_PASSWORD=<a strong temporary passphrase>
```

```bash
docker compose up -d backend        # creates the admin only if no users exist
```

Then **immediately**:

1. Log in and change the password:
   ```bash
   curl -X POST http://localhost:8080/api/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"<temporary>"}'

   curl -X POST http://localhost:8080/api/auth/password \
     -H "Authorization: Bearer <access_token>" \
     -H 'Content-Type: application/json' \
     -d '{"current_password":"<temporary>","new_password":"<real passphrase>"}'
   ```
2. Clear `AUTH_BOOTSTRAP_USERNAME` and `AUTH_BOOTSTRAP_PASSWORD` from `.env`.
3. `docker compose up -d backend` to restart without them.

Startup validation warns for as long as the bootstrap password is present.

---

## 4. Migrations

**Never migrate from the application container.** With more than one replica
each would run migrations concurrently and race. The `migrate` service exists
as a one-shot release step:

```bash
docker compose --profile tools run --rm migrate     # alembic upgrade head
```

Order for an upgrade:

```bash
git pull
docker compose build
docker compose --profile tools run --rm migrate     # 1. schema
docker compose up -d                                # 2. code
```

Roll back one revision:

```bash
docker compose --profile tools run --rm migrate alembic downgrade -1
```

> **M6 note.** Before M6 this command left the database unusable on
> PostgreSQL: `DROP TABLE` does not remove the native `ENUM` types backing
> `sa.Enum`, so rolling forward again failed with
> `DuplicateObject: type "executionstatus" already exists`. Fixed in M6 and
> verified over three full `upgrade -> downgrade -> upgrade` cycles against
> PostgreSQL 16.2. If you are rolling back a database migrated by a pre-M6
> build, check for orphaned types with:
>
> ```sql
> SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
> WHERE n.nspname = 'public' AND t.typtype = 'e';
> ```

Migrations are tested in CI-equivalent form by
`backend/tests/m5/test_migrations_m5.py`, which exercises upgrade → downgrade →
re-upgrade and asserts every ORM table has a migration.

---

## 5. Configuration

`.env.production.example` documents every setting. The critical ones:

| Variable | Required | Why |
| --- | --- | --- |
| `AUTH_ENABLED=true` | yes | `false` in production is a startup error |
| `AUTH_SECRET_KEY` | yes | Token signing. Unique per deployment |
| `DATABASE_URL` | yes | PostgreSQL; SQLite warns |
| `CORS_ORIGINS` | yes | Exact origins. `*` is a startup error |
| `ALLOWED_HOSTS` | recommended | Blocks host-header injection |
| `TRUST_PROXY_HEADERS` | if proxied | Only when a proxy you control sets `X-Forwarded-For` |
| `ENABLE_DOCS=false` | recommended | Do not expose Swagger publicly |
| `SECURITY_HSTS_ENABLED=true` | with TLS | |

### Startup validation

The backend refuses to start in production when configuration is unsafe:

```
Refusing to start: unsafe production configuration.
  - [AUTH_ENABLED] Authentication is disabled: every API caller is treated as a local admin.
```

Audit a running instance (requires `manage_settings`):

```bash
curl -H "Authorization: Bearer <token>" \
  http://localhost:8080/api/system/config/validation
```

`ALLOW_INSECURE_PRODUCTION=true` overrides the refusal. It is logged loudly and
is not a supported configuration.

---

## 6. TLS

The stack serves plain HTTP on `HTTP_PORT` (default 8080). **Terminate TLS in
front of it** — Caddy, Traefik, nginx, or a cloud load balancer.

```nginx
server {
    listen 443 ssl http2;
    server_name creator.example.com;

    ssl_certificate     /etc/letsencrypt/live/creator.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/creator.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Execution streaming is Server-Sent Events.
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

Two things must match:
- set `TRUST_PROXY_HEADERS=true` so rate limiting sees the real client;
- ensure the proxy **overwrites** `X-Forwarded-For` rather than appending to a
  client-supplied value, or clients can spoof their address.

---

## 7. Observability

| Endpoint | Purpose | Auth |
| --- | --- | --- |
| `/health` | V1-compatible check | none |
| `/health/live` | Liveness. Does not touch the database | none |
| `/health/ready` | Readiness. Checks DB, scheduler, workers, config. **503 when degraded** | none |
| `/metrics` | Prometheus exposition | optional |
| `/api/system/errors` | Aggregated recent errors | `view_audit` |

Kubernetes probes:

```yaml
livenessProbe:
  httpGet: { path: /health/live, port: 8000 }
  initialDelaySeconds: 20
  periodSeconds: 30
readinessProbe:
  httpGet: { path: /health/ready, port: 8000 }
  initialDelaySeconds: 10
  periodSeconds: 10
```

Prometheus:

```yaml
scrape_configs:
  - job_name: creator-os
    static_configs:
      - targets: ["creator-os-backend:8000"]
```

Exposed series include `creator_os_http_requests_total`,
`creator_os_http_request_duration_seconds`, `creator_os_executions_total`,
`creator_os_execution_queue_depth` and `creator_os_auth_attempts_total`.

Logs are JSON on stdout (`LOG_FORMAT=json`) with `request_id` and
`correlation_id` on every line. Collect them with your existing agent; the
application does not ship logs itself.

---

## 8. Backup and restore

```bash
# Database
docker compose exec -T db pg_dump -U creator creator_os | gzip > backup-$(date +%F).sql.gz

# Media
docker run --rm -v creator-os_media_data:/data -v "$PWD:/backup" \
  alpine tar czf /backup/media-$(date +%F).tar.gz -C /data .
```

```bash
# Restore
gunzip -c backup-2026-07-26.sql.gz | docker compose exec -T db psql -U creator creator_os
docker run --rm -v creator-os_media_data:/data -v "$PWD:/backup" \
  alpine tar xzf /backup/media-2026-07-26.tar.gz -C /data
```

Back up `.env` separately and securely: without `AUTH_SECRET_KEY` all sessions
are invalidated, and the database is useless without its password.

---

## 9. Scaling — read before increasing replicas

**Creator OS currently runs correctly as a single backend process.** Several
subsystems hold state in process memory:

| Subsystem | Consequence of running >1 process |
| --- | --- |
| Execution queue | Each process keeps its own in-memory queue. Two processes can claim the same queued execution — **double execution**. |
| Rate limiter | Per-process counters, so the effective limit multiplies by process count. **M6 measured this**: `--workers 4` with a 5/min credential budget admitted 15 of 30 attempts — 3x the configured limit. |
| SSE broker | A client connected to replica A sees no events from executions on replica B. |
| AI traces / error aggregation | Per-process views only. |

Keep `WEB_CONCURRENCY=1` and a single `backend` replica until a shared queue
and rate-limit store exist. Scale vertically (`EXECUTION_MAX_WORKERS`,
`WORKFLOW_MAX_PARALLEL_NODES`, CPU/RAM) instead.

### Sizing a single instance (measured in M6)

Every in-flight request holds one database connection for its entire lifetime,
so **connection-pool capacity — not CPU — is what caps concurrency.** Measured
at 100 concurrent authenticated clients against PostgreSQL 16.2:

| `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` | Success | Error rate | Throughput | p99 |
| --- | --- | --- | --- | --- |
| 5 + 10 = 15 (pre-M6 default) | 420/500 | 16.0% | 7.6 rps | 60.5 s |
| 10 + 30 = 40 | 460/500 | 8.0% | 31.2 rps | 10.6 s |
| 20 + 60 = 80 (**current default**) | 500/500 | 0.0% | 81.7 rps | 4.6 s |
| 40 + 80 = 120 | 500/500 | 0.0% | 79.9 rps | — |

Rules of thumb:

* Capacity must exceed the concurrency you intend to serve; 80 handles ~100
  concurrent authenticated requests with zero errors.
* Beyond 80 there is no measurable gain — do not over-provision.
* **`(DB_POOL_SIZE + DB_MAX_OVERFLOW) x replicas` must stay below the
  PostgreSQL `max_connections`** (default 100). Raise `max_connections`, or put
  PgBouncer in front, before adding replicas.
* Overload is shed as `503` with `Retry-After` and the stable error code
  `database_unavailable` — wire your load balancer to honour it.

Reproduce these numbers with `python scripts/loadtest.py`.

---

## 10. Hardening checklist

- [ ] `AUTH_ENABLED=true`, `AUTH_SECRET_KEY` unique and ≥32 chars
- [ ] Bootstrap credentials cleared after first login
- [ ] `CORS_ORIGINS` and `ALLOWED_HOSTS` set to real hostnames
- [ ] `ENABLE_DOCS=false`
- [ ] TLS terminating in front; `SECURITY_HSTS_ENABLED=true`
- [ ] `TRUST_PROXY_HEADERS` matches reality, proxy overwrites `X-Forwarded-For`
- [ ] PostgreSQL, not SQLite; database not published to the host
- [ ] Script executors left `false` unless every author is trusted (see `SECURITY.md` §5)
- [ ] `HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS=false`
- [ ] `/api/system/config/validation` returns zero errors
- [ ] Backups scheduled and a restore rehearsed
- [ ] Metrics scraped, logs collected, `/health/ready` wired to the balancer

---

## 11. Troubleshooting

**Backend exits immediately with "Refusing to start".** Startup validation
rejected the configuration; the log lists each key and its remediation.

**`/health/ready` returns 503.** Read `checks` in the body — `database`,
`scheduler`, `execution_workers` or `configuration` will identify the cause.

**Executions stay `QUEUED`.** The worker pool did not start; check
`execution_workers` in readiness and the startup logs. Note that queued runs do
**not** survive a restart (`KNOWN_ISSUES.md` #1).

**Live execution updates never arrive.** Something between client and backend
is buffering the SSE stream. Ensure `proxy_buffering off` and a long
`proxy_read_timeout` at every proxy hop.

**429s under normal load.** Raise `RATE_LIMIT_REQUESTS`, or check whether
`TRUST_PROXY_HEADERS` is false behind a proxy — every client would then share
one bucket keyed by the proxy's address.

**503 `database_unavailable` under load.** The connection pool is exhausted:
more requests are in flight than there are connections. Raise
`DB_POOL_SIZE`/`DB_MAX_OVERFLOW` (see §9 sizing) and confirm PostgreSQL
`max_connections` has room. This is load shedding working as intended, not a
crash — the process stays live and `/health/live` keeps answering.

**Backend will not start after editing `.env`.** Pre-M6 builds could not parse
comma-separated `CORS_ORIGINS`/`ALLOWED_HOSTS` and died with an opaque
`SettingsError` before logging started. M6 fixed this; both CSV and JSON array
forms now work. If you are on an older build, use the JSON form:
`CORS_ORIGINS=["https://studio.example.com"]`.
