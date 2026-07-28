# Creator OS

**Visual workflow automation with a built-in AI runtime.** Build directed
graphs of AI calls, HTTP requests, media processing and scripted logic in a
drag-and-drop editor, then run them with retries, branching, loops, live
streaming and a full execution history.

Runs two ways from one codebase: a **local desktop app** (SQLite, zero
configuration) or a **multi-user server** (PostgreSQL, JWT auth, RBAC, metrics).

**Version 1.1.1** · Security patch release · [Release notes](docs/RELEASE_NOTES.md) · [Known issues](docs/KNOWN_ISSUES.md) · [Post-v1.1.0 security audit](docs/POST_V110_AUDIT.md)

---

## Quick start

**Prerequisites:** Python 3.11+, Node 22+, ~500 MB disk. Nothing else is
required — PostgreSQL, Docker, FFmpeg and Ollama are all optional.

```bash
git clone https://github.com/sameershinde6293/Automation-Studio.git
cd Automation-Studio

# --- backend (terminal 1) ---
cd backend
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/alembic upgrade head
./.venv/bin/uvicorn app.main:app --reload

# --- frontend (terminal 2) ---
cd frontend
ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm install
npm run dev
```

Open **http://localhost:5173**. The API is on **http://localhost:8000**, with
interactive docs at `/docs`.

```bash
curl http://localhost:8000/health        # {"status":"healthy"}
```

Run `uvicorn` against `app.main:app` — **not** `main:app`. `backend/main.py` is
a V1.0 stub that serves an app with no routers.

Full walkthrough: **[docs/INSTALLATION_GUIDE.md](docs/INSTALLATION_GUIDE.md)**.

### Try it immediately

```bash
python scripts/verify_examples.py
```

Imports, runs and exports all four [example workflows](examples/README.md)
against your running backend. All four are verified in CI-equivalent form on
every release.

---

## What it does

| | |
| --- | --- |
| **Workflow engine** | DAG execution with branch gating, loops, per-node retry/backoff, timeouts, pause/resume/stop, replay and resume-failed |
| **Visual editor** | React Flow canvas — 22 node types, drag-to-connect, cycle prevention, keyboard shortcuts, clipboard, undo/redo, autosave |
| **AI runtime** | OpenAI, Ollama and a built-in mock provider behind one interface, with an ordered fallback chain, circuit breaker, conversation memory, token accounting and cost tracking |
| **Node library** | AI chat/completion, prompt templates, HTTP, webhooks, conditions, loops, delays, variables, transforms, file/folder I/O, email, SQL, FFmpeg, TTS/STT, image generation, Python and JavaScript |
| **Execution visibility** | Live Server-Sent Events streaming, durable per-node logs, searchable history, metrics |
| **Media pipeline** | Background workers, FFmpeg probe/transcode/poster with graceful degradation when FFmpeg is absent |
| **Security** | JWT auth, API keys with scope intersection, PBKDF2 hashing, per-endpoint RBAC, CSRF, trusted hosts, HSTS, rate limiting, SSRF guards, a process-isolated script sandbox with kernel resource limits |
| **Operations** | Liveness/readiness probes, Prometheus metrics, JSON logs with request correlation and secret redaction, startup configuration validation, Alembic migrations |

---

## Deployment

**Local desktop** — SQLite, no auth, no configuration. The quick start above.

**Server** — PostgreSQL, authentication on, TLS terminated in front.

```bash
cp .env.production.example .env
# set AUTH_SECRET_KEY and POSTGRES_PASSWORD — both are mandatory
docker compose --profile tools run --rm migrate
docker compose up -d
```

> **Docker is validated statically only — it has never been run.** Across M5,
> M6, M7, M8, M9 and now M10 no container runtime has existed in the validation
> environment (no `docker`/`podman`/`nerdctl`, no `/var/run/docker.sock`, and
> `registry-1.docker.io` and `ghcr.io` are both unreachable). What *is* verified:
> 44 static checks + 53 asset tests covering multi-stage builds, non-root USER,
> HEALTHCHECK liveness/readiness, the `creator-os-net` bridge network, json-file
> log rotation 10m x5, `no-new-privileges`, resource limits, the `${VAR}`
> contract against `.env.production.example`, and nginx proxying to
> `backend:8000` with SSE buffering off. Every *process* the containers would
> run has been executed outside them against the same PostgreSQL 16.2 — same
> production settings, same Uvicorn command line, same probes, same migration
> and backup/restore commands. **That is not a substitute for running it.**
> Treat your first containerised deployment as a validation exercise and use
> `scripts/deploy.sh`.
> See [docs/M10_RELEASE_CERTIFICATION.md](docs/M10_RELEASE_CERTIFICATION.md) and [docs/M8_VALIDATION_REPORT.md](docs/M8_VALIDATION_REPORT.md).

The **source + PostgreSQL** path is fully verified. Full procedure, sizing data
and hardening checklist: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** and **[docs/M8_VALIDATION_REPORT.md](docs/M8_VALIDATION_REPORT.md)**.

---

## Project status

**v1.1.0 General Availability.** Re-verified from scratch on 2026-07-28 (M10)
on a production-shaped deployment running against **real PostgreSQL 16.2**.
Every number below was measured in the M10 certification run, not carried
forward from an earlier milestone:

| | |
| --- | --- |
| Backend tests | **1594 passed / 10 skipped** (SQLite) · **1602 passed / 2 skipped** (PostgreSQL 16.2, executed this cycle) · 0 failed |
| PostgreSQL migration suite | **8/8 executed** against real PostgreSQL 16.2 |
| Frontend tests | **179 passed** (13 files) |
| Typecheck / production build | clean · 343.85 kB (109.08 kB gzip), 1735 modules |
| Migrations | upgrade → downgrade to base → re-upgrade on PostgreSQL: **0 orphaned enum types**, 19 tables restored |
| Examples | 4/4 executed against an authenticated production backend on PostgreSQL (M10 figure — not re-run) |
| Performance | `/health` p50 2.7 ms / p95 3.4 ms · startup 41 ms · graceful SIGTERM shutdown clean (M10 figures — not re-measured) |
| Failure testing | DB loss → **503 ready / 200 live**, recovers in **~1 s without restart** · SIGKILL leaves **no orphaned executions** |
| Backup / restore | **Disaster-recovery drill executed**: 16 KB `pg_dump` → `DROP SCHEMA CASCADE` → restore → 20 tables, all rows and migration state intact, app authenticates and serves 200 |
| Security posture | `/docs` 404 · unauthenticated API 401 · bad Host 400 · secrets appear **0 times** in logs |
| Docker | **Static only** — 44 checks + 53 asset tests pass; runtime **never executed** (no container runtime, registries unreachable) — 5th consecutive milestone |
| CI | `ci/github-actions-ci.yml` present; `.github/workflows/` **cannot be pushed** by this app — requires maintainer activation |
| Observability | `/health`, `/health/live`, `/health/ready`, `/metrics` (**14 metric families** confirmed live, incl. DB pool gauges), JSON logs with correlation IDs, audit log |

**Production readiness: 94%.** Held below 98% because the Docker deployment
path has still never been executed here, multi-replica operation is untested,
no 24-hour soak has been run, and CI has never executed. Full evidence and the
defects found in this milestone:
**[docs/M10_RELEASE_CERTIFICATION.md](docs/M10_RELEASE_CERTIFICATION.md)** (current), **[docs/M9_VALIDATION_REPORT.md](docs/M9_VALIDATION_REPORT.md)**.

---

## Documentation

**Start here**
[Installation](docs/INSTALLATION_GUIDE.md) ·
[Examples](examples/README.md) ·
[User guide](docs/USER_GUIDE.md) ·
[Troubleshooting](docs/TROUBLESHOOTING.md) ·
[FAQ](docs/FAQ.md)

**Operating it**
[Deployment](docs/DEPLOYMENT.md) ·
[Upgrade guide](docs/UPGRADE_GUIDE.md) ·
[Security](docs/SECURITY.md) ·
[Known issues](docs/KNOWN_ISSUES.md)

**Building on it**
[Architecture](docs/ARCHITECTURE.md) ·
[API reference](docs/API_DOCUMENTATION.md) ·
[Execution engine](docs/EXECUTION_ENGINE.md) ·
[Workflow editor](docs/WORKFLOW_EDITOR.md) ·
[Developer guide](docs/DEVELOPER_GUIDE.md) ·
[Contributing](CONTRIBUTING.md)

**Release**
[Release notes](docs/RELEASE_NOTES.md) ·
[Changelog](docs/CHANGELOG.md) ·
[Project status](docs/PROJECT_STATUS.md) ·
[M10 release certification](docs/M10_RELEASE_CERTIFICATION.md) ·
[Release checklists](docs/RELEASE_CHECKLIST.md) ·
[M7 release audit](docs/M7_RELEASE_AUDIT.md)

---

## Testing

```bash
./scripts/ci-local.sh        # everything CI would run

cd backend && ./.venv/bin/python -m pytest -q            # backend
cd frontend && npm test && npm run typecheck             # frontend
python scripts/verify_examples.py                        # examples
```

To include the PostgreSQL migration tests (skipped by default):

```bash
TEST_POSTGRES_URL=postgresql+psycopg://user:pass@localhost:5432/scratch \
  ./.venv/bin/python -m pytest tests/m6/test_postgres_migrations_m6.py
```

CI is defined in `ci/github-actions-ci.yml` (7 jobs, including Docker build
inspection, example verification and production-build). **It has never
executed.** GitHub only runs workflows from `.github/workflows/`, and pushing
there is rejected for this automation account (`refusing to allow a GitHub App
to create or update workflow ... without 'workflows' permission`). A maintainer
must activate it:

```bash
mkdir -p .github/workflows
cp ci/github-actions-ci.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml && git commit -m "ci: activate pipeline" && git push
```

Until then `./scripts/ci-local.sh` runs the same checks locally. See
`ci/README.md`.

---

## Architecture

```
frontend/          React 18 + TypeScript + Vite + React Flow; Electron shell
  src/components/  Canvas, node palette, properties, execution panel, 22 nodes
  src/stores/      Zustand state + backend sync

backend/           FastAPI + SQLAlchemy 2 + Alembic
  app/api/         Routers, schemas, auth dependencies
  app/domain/      ORM models and repositories
  app/services/    Workflow engine, AI orchestration, media, security, plugins
  app/infrastructure/  Config, database, logging, metrics, scheduler
  tests/           1594 tests (1602 with PostgreSQL enabled)

examples/          Executable example workflows
scripts/           Build, local CI, smoke tests, load test, example verifier
docs/              Documentation
```

Clean-architecture layering: domain logic does not import infrastructure.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## License

No license file is currently present. Until one is added, all rights are
reserved by the repository owner — see [docs/FAQ.md](docs/FAQ.md).
