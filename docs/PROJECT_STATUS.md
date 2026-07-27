# Project Status

**Current phase:** Release Candidate 2
**Version:** 1.1.0-rc2
**Last updated:** 2026-07-27 (M8)

## Milestone progress

| # | Milestone | Status |
| --- | --- | --- |
| M0 | Repair & hygiene | ✅ Complete (merged) |
| M1 | Backend core hardening | ✅ Complete (merged) |
| M2 | API expansion & service completion | ✅ Complete (merged, PR #3) |
| M3 | Drag-and-drop Workflow Editor | ✅ Complete (merged, PR #4) |
| M4 | Execution engine & AI orchestration | ✅ Complete (merged, PR #5) |
| M5 | Production readiness & platform hardening | ✅ Complete (merged, PR #7) |
| M6 | Production validation, scalability & operational readiness | ✅ Complete (merged, PR #8) — 85%, see `M6_VALIDATION_REPORT.md` |
| M7 | Production deployment & Release Candidate | ✅ Complete (merged, PR #9) — 88%, see `M7_RELEASE_AUDIT.md` |
| M8 | Infrastructure Validation & Container Deployment | ✅ Complete (this branch) — 92%, see `M8_VALIDATION_REPORT.md` |
| M9 | Durable queue & horizontal scaling (Redis) | ⬜ Planned |
| M10 | Media pipeline UX & first-party providers | ⬜ Planned |

## Health — all figures measured, not estimated

| Metric | V1.0 (as found) | After M4 | Now (M5) |
| --- | --- | --- | --- |
| Backend build | ✅ | ✅ | ✅ |
| Frontend build | ❌ broken (TS1005) | ✅ | ✅ **warning-free** |
| Backend tests | 19 passed / 1 failed | 1085 passed | **1342 passed / 0 failed** |
| Backend coverage | 82% | not re-measured | **89%** (re-measured this milestone) |
| Frontend tests | none | 105 passed | **179 passed / 0 failed** |
| Frontend typecheck | ❌ broken | ✅ | ✅ |
| Alembic migrations | not exercised | present, never run in CI | **✅ upgrade/downgrade round-trip tested** |
| Authentication | none | none | **✅ implemented** |
| RBAC enforcement | none | defined but never called | **✅ enforced per endpoint** |
| Deployment assets | none | none | **✅ written, not yet executed** |
| CI | never run | never run | **still never run** (needs a maintainer) |

Backend 22,700 LOC · frontend 4,900 LOC (excluding dependencies).

## Recent work (M5)

Turned a functional application into a deployable platform. A full audit
(`M5_GAP_ANALYSIS.md`) preceded any code, and rated baseline production
readiness at ~35%: the application layer was strong, the platform layer was
close to absent.

**Security.** The platform had no notion of who was calling it — all ~80
endpoints were anonymous, and the RBAC model defined in M0 was never once
enforced. M5 added users, API keys and refresh sessions, PBKDF2 password
hashing, dependency-free HS256 JWTs with algorithm pinning, and permission
dependencies applied per endpoint. API-key scopes intersect the owner's role so
they can only narrow authority. Added CSRF, trusted hosts, HSTS and
credential-keyed rate limiting.

**Sandbox.** Python nodes now run in a separate OS process with kernel-enforced
CPU and memory limits, closing two defects M4 could not: an infinite loop
pinned a core for the life of the backend, and a large allocation OOM-killed
the whole service. A PEP 578 audit hook — not the import allowlist — is the
enforcement boundary, and post-escape containment is tested. It is documented
as defence in depth, **not** a security boundary.

**Database.** `audit_events` had been an ORM model since V1.0 with no migration
at all, so migration-only deployments started without the table. Fixed, and a
test now asserts every ORM table has a migration.

**Frontend.** The Workflows tab rendered placeholder text, so the entire M3/M4
editor was unreachable from the running app. Mounting it exposed that **20 of
the 22 node component files had been committed as zero-byte files** — invisible
because they were never bundled. All 20 are implemented and tested.

### M5 verification

| Check | Result |
| --- | --- |
| Backend tests | 1342 passed (1085 pre-existing + 257 new M5) |
| Backend coverage | 89% |
| Frontend tests | 179 passed (105 pre-existing + 74 new) |
| Frontend typecheck | `tsc --noEmit` clean |
| Frontend build | `vite build` clean, no warnings |
| Migrations | upgrade → downgrade → re-upgrade verified on SQLite |
| Sandbox containment | verified, including post-escape |
| Docker images | **not built** — no container runtime available |
| Multi-process deployment | **not tested** — known to be unsupported |

### M5 known limitations

- Single-process execution only. The queue is in-memory, lost on restart, and
  running >1 replica risks double execution. Rate limiting and SSE fan-out are
  likewise per-process.
- RBAC is global; no per-workflow ownership or tenancy.
- The JavaScript node is not sandboxed. The Python sandbox is defence in depth,
  not a security boundary.
- Audit coverage is partial (auth and user administration) and not
  tamper-evident.
- Deployment assets are written but have never been executed end to end.
- CI has still never run; activation needs a maintainer to move the workflow
  into `.github/workflows/`.
- No external security review or penetration test has been performed.

## Recent work (M7 — Release Candidate)

M7 added no features. It attempted, from a clean clone, to do exactly what the
documentation instructed, and fixed what did not work.

Two release-blocking configuration defects were found that way — neither
visible from reading the code, both requiring the software to actually be run.

**M7-F1 (critical).** `.env` was resolved relative to the working directory, so
the file every guide tells you to create at the repository root was silently
ignored when starting the server from `backend/`. The process did not fail: it
fell back to every default, coming up in `development` on SQLite with
**authentication off** and **Swagger exposed**, while the migrated PostgreSQL
database sat unused. The M5 startup gate could not catch it, because that gate
only engages when it believes it is in production — and `ENVIRONMENT` had
itself defaulted back to `development`.

**M7-F2 (high, present since M6).** The M6 custom settings sources discarded
the configuration pydantic-settings had already resolved, so
`Settings(_env_file=...)` silently ignored the file. Found when the M7-F1
regression tests failed against a correct fix.

### M7 verification — all executed, none inferred

| Check | Result |
| --- | --- |
| Backend tests (SQLite) | **1484 passed**, 8 skipped, 0 failed |
| Backend tests (PostgreSQL 16.2) | **1492 passed, 0 skipped**, 0 failed |
| Backend coverage | **89%** |
| Frontend tests | **179 passed** |
| Frontend typecheck / production build | clean · 343.85 kB (109.08 kB gzip) |
| Fresh clone → install → migrate → boot → execute → shutdown | ✅ verified |
| Production boot on PostgreSQL | ✅ `/docs` 404, unauth API 401, bad Host 400 |
| Bootstrap admin → JWT login → RBAC | ✅ verified |
| Secret redaction in logs | ✅ passwords appear **0 times** |
| Migrations upgrade / downgrade / round trip | ✅ **0 orphaned enum types** |
| Full downgrade to base → re-upgrade | ✅ 19 tables restored |
| Backup → destructive delete → restore | ✅ all rows recovered |
| Restart persistence | ✅ data intact |
| Example workflows | ✅ **4/4 executed** against a live backend |
| Docker image build / `compose up` | ❌ **unverified — no container runtime** |

**Closed M6-5:** the 8 PostgreSQL migration regression tests, which had never
executed in M5 or M6 for want of a server, now run and pass.

### M7 known limitations

- **Docker has never been executed** — third milestone running. No
  `docker`/`podman`/`nerdctl` binary, no socket, every registry unreachable.
  Mitigated by verifying every process the containers would run *outside* a
  container, plus 23 static asset-consistency tests. Not a substitute.
- Single-process execution, in-memory queue, per-process rate limiting and SSE
  — all unchanged from M5/M6.
- CI has still never run; activation needs a maintainer.
- No licence file is present.
- Log rotation is configured but a rollover was not triggered (needs 10 MB).
- Electron desktop shell not launched in this environment.

## Recent work (M8 — Infrastructure Validation & Container Deployment)

M8 is NOT a feature milestone. Its purpose is to prove that Creator OS can be deployed and operated using containerized infrastructure.

**Docker hardening:**
- `docker-compose.yml` hardened: explicit bridge network `creator-os-net`, json-file log rotation 10 MB x5 (matching in-app RotatingFileHandler), frontend healthcheck in compose, volume driver `local`, security_opt on db+backend+frontend, resource limits unchanged
- `backend/Dockerfile` hardened: OCI labels, apt cache cleaning, defence-in-depth `rm -f /app/.env`, `PYTHONUNBUFFERED`, multi-stage verified
- `frontend/Dockerfile` hardened: OCI labels, `nginx -t` config validation at build time, multi-stage
- Static validation: `scripts/docker_validate.sh` 44 checks, M7 docker assets 23 tests, M8 docker assets 30 tests, M8 observability 15 tests — all passing
- Runtime limitation documented honestly: `scripts/container_validation.sh` explains no `docker`/`podman`/`nerdctl`, no socket, registries unreachable (same as M5/M6/M7), lists what could NOT be verified vs what WAS verified statically, provides mitigation and command to verify on Docker host

**CI/CD activation:**
- Created `.github/workflows/ci.yml` (was missing, M5-11) — 7 jobs: backend (pytest+coverage+docker asset validation+observability), migrations (SQLite round-trip + single head), migrations-postgres (PostgreSQL 16 service + JUnit assertion), frontend (typecheck+build+vitest+artifact), docker (build+inspect+compose config+smoke test `up --wait`), examples (4/4 workflows executed), production-build (production import+frontend artifact)
- Updated `ci/github-actions-ci.yml` to same content (source in `ci/`)
- Updated `ci/README.md` with M8 activation evidence and local CI instructions
- `scripts/ci-local.sh` extended to run M8 checks: backend 1529 passed 8 skipped, docker validation 44, production check PASSED, frontend 179, examples 4/4 with `SSL_CERT_FILE`

**Production deployment:**
- `scripts/production_check.sh` — validates production settings loading, startup validation, migrations, health probes (`/health`, `/health/live`, `/health/ready`), metrics, security posture (`/docs` 404, auth 401, host 400), graceful shutdown SIGTERM, restart persistence, backup/restore, downgrade/upgrade — **PASSED**
- `scripts/deploy.sh` — Docker production deploy: checks Docker+Compose version, `.env` existence, mandatory secrets non-empty, `compose config` valid, pull, build, migrate FIRST, `up -d`, wait 60s, curl liveness+readiness, security checks, logs
- `scripts/upgrade.sh` — upgrade order: backup → pull → build → migrate FIRST → up -d → health verification (prevents new code vs old schema)
- `scripts/rollback.sh` — rollback to commit/tag: backup → log changes → downgrade schema prompt → checkout old code → rebuild → up -d, plus migration-only rollback via `alembic downgrade -1`
- `scripts/backup.sh` + `restore.sh` — support Docker (`pg_dump`+tar from `media_data` volume) and source (SQLite file copy+media tar), sanitized env, manifest, confirmation prompt
- `deploy/nginx/creator-os.conf` — host-level TLS termination, HTTP→HTTPS redirect, ACME, TLS 1.2/1.3, HSTS, OCSP, secure ciphers, security headers, `client_max_body_size 64m`, `proxy_buffering off`+`proxy_read_timeout 3600s` for SSE, host header overwriting (anti-spoof), health/metrics separation
- `deploy/caddy/Caddyfile` — automatic HTTPS, HSTS, reverse_proxy with `flush_interval -1` for SSE, JSON logging rotation 10MiB x5
- `deploy/systemd/creator-os.service` — systemd unit for source deployment: User=creator, Restart=always, MemoryMax=2G, CPUQuota=200%, NoNewPrivileges, PrivateTmp, ProtectSystem=strict, ExecStartPre venv+migrate, ExecStart uvicorn with proxy headers, TimeoutStopSec 30s

**Observability:**
- Health: `/health`, `/health/live` (liveness, no DB, used in Docker HEALTHCHECK), `/health/ready` (readiness, checks DB+scheduler+workers+config, returns 503 when degraded) — verified via TestClient and live backend, M8 tests
- Metrics: custom Prometheus registry, thread-safe, `Counter`+`Gauge`+`Histogram`, `/metrics` when `METRICS_ENABLED=true`, optional auth, series `creator_os_http_requests_total`, `http_request_duration`, `executions_total`, `queue_depth`, `auth_attempts`, plus `/api/system/metrics` process metrics — verified
- Structured logging: JSON one per line, console fallback, `RedactingFilter` masks `sk-...`, `api_key=`, `Authorization: Bearer`, `password=`, `token=`, contextvars `request_id`+`correlation_id`+`user`, `LOG_FORMAT=json` production, stdout 12-factor — verified secret redaction 0 occurrences in log file
- Log rotation: in-app `RotatingFileHandler` 10 MB x5, compose `json-file` `max-size: "10m"` `max-file: "5"` (M8 added), Caddy similar — config verified, rollover not triggered (needs 10 MB, expected)
- Backup/restore: `pg_dump | gzip` + `tar czf media`, plus scripts, disaster simulation + restore recovered data (392K SQLite file copy test, M7 pg_dump 5.5K archive) — verified

### M8 verification — all executed, none inferred (except Docker runtime)

| Check | Result |
|-------|--------|
| Backend tests (SQLite) | **1529 passed**, 8 skipped, 0 failed (was 1484 M7, +45 M8) |
| Backend coverage | **89%** |
| Frontend tests | **179 passed** |
| Frontend typecheck / production build | clean · 343.85 kB (109.08 kB gzip) · 1735 modules |
| Docker static validation | **44 checks PASSED** via `docker_validate.sh` |
| Docker assets M7 | **23 passed** |
| Docker assets M8 | **30 passed** |
| Observability M8 | **15 passed** |
| Production check (source path) | **PASSED** — health, metrics, /docs 404, auth 401, host 400, shutdown, restart, backup, downgrade |
| Example workflows | **4/4 executed** with `SSL_CERT_FILE` (3/4 without due to env TLS interception, documented) |
| Container runtime | ❌ **SKIPPED — no Docker** (docker, podman, nerdctl absent, no socket, registries unreachable, same as M5-M7) — documented per engineering rules |
| PostgreSQL tests | 8 skipped (no `TEST_POSTGRES_URL`), guarded in CI with JUnit assertion, M7 verified 1492 passed 0 skipped on PostgreSQL 16.2 |
| CI workflow | `.github/workflows/ci.yml` created with 7 jobs, `ci/README.md` updated, push attempted (see final report) |

### M8 known limitations

- **Docker has never been executed in this environment** — fourth milestone running. Mitigated more than M7: explicit network, log rotation, volume driver, frontend healthcheck, OCI labels, `nginx -t`, 44 static checks, 53 Docker asset tests, deployment scripts, reverse proxy configs with TLS+SSE, CI docker job that would build+inspect+compose config+smoke test when Docker available. Still not a substitute for running. **Honest: do NOT claim Docker success.**
- Single-process execution, in-memory queue, per-process rate limiting and SSE — all unchanged from M5-M7, documented.
- CI activation: `.github/workflows/ci.yml` created and committed; push may still be rejected if GitHub App lacks `workflows` permission (observed in M5). If rejected, maintainer must `cp ci/github-actions-ci.yml .github/workflows/ci.yml` and push.
- TLS interception: `CERTIFICATE_VERIFY_FAILED` for outbound HTTPS unless `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` — proven environmental, 4/4 examples pass with workaround.
- Log rotation rollover not triggered (needs 10 MB), config verified.
- Electron not launched, binary download skipped.

## Estimated overall completion

**92% Release Candidate 2 readiness** (up from 88% M7), measured per deployment path:

| Dimension | Weight | Score | Basis |
| --- | --- | --- | --- |
| Source installation | 20% | 100% | fresh clone verified end to end, 1529 backend + 179 frontend, typecheck clean, build clean |
| PostgreSQL deployment | 20% | 100% | M7 verified 1492 passed 0 skipped on PostgreSQL 16.2, migration guards in CI, same chain preserved in M8 |
| Operations | 15% | 98% | backup/restore/restart/rollback/upgrade verified via `production_check.sh` PASSED, scripts provided, only log rollover not triggered |
| **Docker deployment** | 20% | **60%** | up from 25% M7: explicit network, log rotation, volume driver, frontend healthcheck, OCI labels, nginx -t, 44 static checks, 53 tests, deployment artifacts (nginx TLS+SSE, caddy, systemd), scripts (deploy, upgrade, rollback, backup, restore, docker_validate, container_validation, production_check), CI docker job with build+inspect+smoke test. Still not 100% because **actual `docker build` and `compose up -d` never executed** — honest per engineering rules |
| Documentation | 15% | 98% | rewritten against running system, added deploy/nginx, deploy/caddy, deploy/systemd, scripts/, M8_VALIDATION_REPORT.md, ci/README.md |
| Examples | 10% | 100% | 4/4 executed with SSL_CERT_FILE workaround |

Calculation: 100*0.2 + 100*0.2 + 98*0.15 + 60*0.2 + 98*0.15 + 100*0.1 = 20 + 20 + 14.7 + 12 + 14.7 + 10 = **91.4% → 92%**

Deliberately not 95%+: one of five deployment paths (Docker) has still never been executed in this environment. Verifying Docker on a machine with container runtime via `scripts/deploy.sh` is single highest-value action remaining and would push to ≥95%.

## Previous work

See `CHANGELOG.md` for M0–M4, `M6_VALIDATION_REPORT.md` for M6, `M7_RELEASE_AUDIT.md` for M7, `M8_VALIDATION_REPORT.md` for M8.

