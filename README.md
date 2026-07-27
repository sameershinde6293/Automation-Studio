# Creator OS

**Visual workflow automation with a built-in AI runtime.** Build directed
graphs of AI calls, HTTP requests, media processing and scripted logic in a
drag-and-drop editor, then run them with retries, branching, loops, live
streaming and a full execution history.

Runs two ways from one codebase: a **local desktop app** (SQLite, zero
configuration) or a **multi-user server** (PostgreSQL, JWT auth, RBAC, metrics).

**Version 1.1.0-rc2** · Release Candidate 2 · [Release notes](docs/RELEASE_NOTES.md) · [Known issues](docs/KNOWN_ISSUES.md) · [M8 validation](docs/M8_VALIDATION_REPORT.md)

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

> **Docker is partially validated in M8.** Static validation expanded to 44 checks + 53 tests (23 M7 + 30 M8): multi-stage builds, non-root USER, HEALTHCHECK liveness/readiness, explicit bridge network `creator-os-net`, log rotation json-file 10m x5, volume driver local, security_opt no-new-privileges, resource limits, env var contract, nginx proxies to backend:8000 with SSE buffering off. The images and compose stack have still **never been executed** in this environment — no container runtime has been available in M5, M6, M7 or M8 (no docker/podman/nerdctl, no socket, registries unreachable). Every process the containers would run *has* been verified outside them (same PostgreSQL 16.2, same production settings, same Uvicorn command line, same probes), plus production deployment scripts (`deploy.sh`, `upgrade.sh`, `rollback.sh`, `backup.sh`, `restore.sh`, `production_check.sh`) and reverse proxy configs (nginx with TLS+S SSE, caddy, systemd). Treat your first containerised deployment as a validation exercise using `scripts/deploy.sh`.
> See [docs/M8_VALIDATION_REPORT.md](docs/M8_VALIDATION_REPORT.md) and [docs/M7_RELEASE_AUDIT.md §6](docs/M7_RELEASE_AUDIT.md).

The **source + PostgreSQL** path is fully verified. Full procedure, sizing data
and hardening checklist: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** and **[docs/M8_VALIDATION_REPORT.md](docs/M8_VALIDATION_REPORT.md)**.

---

## Project status

Release Candidate 2. Verified on 2026-07-27 (M8):

| | |
| --- | --- |
| Backend tests | **1529 passed / 8 skipped** (SQLite, +45 M8) · **1492 passed, 0 skipped** (PostgreSQL 16.2, M7) |
| Backend coverage | **89%** |
| Frontend tests | **179 passed** |
| Typecheck / production build | clean · 343.85 kB (109.08 kB gzip) |
| Migrations | upgrade → downgrade → re-upgrade verified (SQLite + PostgreSQL M7) |
| Examples | **4/4 executed** against live backend (with SSL_CERT_FILE workaround for TLS interception) |
| Docker | **Partially validated** — 44 static checks + 53 docker asset tests (23 M7 + 30 M8), explicit network, log rotation, volume driver, OCI labels, nginx -t; runtime **still requires Docker host** — see M8 report §1.6 |
| CI | **Activated** in M8 — `.github/workflows/ci.yml` with 7 jobs (backend, migrations, migrations-postgres, frontend, docker, examples, production-build) |
| Deployment scripts | `deploy.sh`, `upgrade.sh`, `rollback.sh`, `backup.sh`, `restore.sh`, `production_check.sh`, `docker_validate.sh`, `container_validation.sh` — source path PASSED |
| Observability | `/health`, `/health/live`, `/health/ready`, `/metrics`, JSON logs with redaction, log rotation 10m x5, backup/restore — verified |

**Readiness: 92%** (up from 88% M7). Not higher, because Docker runtime has still never been executed in this environment — honest per engineering rules. Details and full evidence:
**[docs/M8_VALIDATION_REPORT.md](docs/M8_VALIDATION_REPORT.md)** (current), **[docs/M7_RELEASE_AUDIT.md](docs/M7_RELEASE_AUDIT.md)**.

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

CI is defined in `.github/workflows/ci.yml` (activated in M8) and `ci/github-actions-ci.yml` (source). The workflow now has 7 jobs including Docker build inspection, example verification, and production-build. See `ci/README.md` and `docs/M8_VALIDATION_REPORT.md` §2.

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
  tests/           1492 tests

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
