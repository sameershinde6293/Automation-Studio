# Creator OS

**Visual workflow automation with a built-in AI runtime.** Build directed
graphs of AI calls, HTTP requests, media processing and scripted logic in a
drag-and-drop editor, then run them with retries, branching, loops, live
streaming and a full execution history.

Runs two ways from one codebase: a **local desktop app** (SQLite, zero
configuration) or a **multi-user server** (PostgreSQL, JWT auth, RBAC, metrics).

**Version 1.1.0-rc1** · Release Candidate · [Release notes](docs/RELEASE_NOTES.md) · [Known issues](docs/KNOWN_ISSUES.md)

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

> **Docker is unverified.** The images and compose stack have never been
> executed — no container runtime has been available in M5, M6 or M7. Every
> process the containers would run *has* been verified outside them (same
> PostgreSQL 16.2, same production settings, same Uvicorn command line, same
> probes), and the assets are statically validated by 23 tests. Treat your first
> containerised deployment as a validation exercise.
> See [docs/M7_RELEASE_AUDIT.md §6](docs/M7_RELEASE_AUDIT.md).

The **source + PostgreSQL** path is fully verified. Full procedure, sizing data
and hardening checklist: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

---

## Project status

Release Candidate 1. Verified on 2026-07-27:

| | |
| --- | --- |
| Backend tests | **1487 passed** (SQLite) · **1446 passed, 0 skipped** (PostgreSQL 16.2) |
| Backend coverage | **89%** |
| Frontend tests | **179 passed** |
| Typecheck / production build | clean |
| Migrations | upgrade → downgrade → re-upgrade verified on PostgreSQL |
| Examples | **4/4 executed** against a live backend |
| Docker | ❌ **unverified** — no runtime available |

**Readiness: 88%.** Not higher, because one of the five documented deployment
paths (Docker) has never been executed. Details and the full evidence trail:
**[docs/M7_RELEASE_AUDIT.md](docs/M7_RELEASE_AUDIT.md)**.

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

CI is defined in `ci/github-actions-ci.yml` but **has never run** — it needs a
maintainer to copy it into `.github/workflows/` (see `ci/README.md`).

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
  tests/           1487 tests

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
