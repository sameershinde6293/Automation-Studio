# Deployment

Creator OS v1.1 · last updated 2026-07-26 (M5)

How to run Creator OS as a server. For the local desktop application see
`INSTALLATION_GUIDE.md`.

> **Status.** These assets are new in M5. The Dockerfiles, compose stack and
> procedures below are written and reviewed, but **they have not been executed
> end to end** — no container runtime was available in the environment where
> M5 was developed. Treat the first deployment as a validation exercise.
> `docker-compose.yml` was verified to parse and the Postgres driver was
> verified to resolve, but the images have never been built or run.

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
| Rate limiter | Per-process counters, so the effective limit multiplies by process count. |
| SSE broker | A client connected to replica A sees no events from executions on replica B. |
| AI traces / error aggregation | Per-process views only. |

Keep `WEB_CONCURRENCY=1` and a single `backend` replica until a shared queue
and rate-limit store exist. Scale vertically (`EXECUTION_MAX_WORKERS`,
`WORKFLOW_MAX_PARALLEL_NODES`, CPU/RAM) instead.

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
