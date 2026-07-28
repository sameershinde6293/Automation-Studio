# Installation Guide

Creator OS v1.1.1 · last verified 2026-07-28

Every command below was executed from a clean clone on Linux with Python 3.11.2
and Node 22.22.3 during the M7 release audit. Where something is unverified, it
says so.

---

## 1. Requirements

| | Minimum | Notes |
| --- | --- | --- |
| **Python** | 3.11+ | 3.11.2 verified |
| **Node.js** | 22+ | 22.22.3 verified. Only needed for the UI |
| **Disk** | ~500 MB | venv ≈ 200 MB, `node_modules` ≈ 250 MB |
| **RAM** | 2 GB | 4 GB for comfortable media work |
| **OS** | Linux, macOS 11+, Windows 10/11 | Linux verified; the script sandbox needs POSIX `setrlimit` and silently degrades on Windows |

**Optional, and genuinely optional** — the app starts and runs without all of
these:

| | For | Without it |
| --- | --- | --- |
| PostgreSQL 14+ | multi-user server deployments | SQLite is used; fine for desktop, not for a server |
| FFmpeg / ffprobe | video and audio processing | Image handling still works via Pillow; media nodes degrade gracefully |
| Ollama | local LLMs | Use OpenAI, or the built-in `mock` provider |
| OpenAI API key | cloud LLMs | Use Ollama, or `mock` |
| Docker 24+ | container deployment | Run from source instead — **and note the Docker path is unverified**, see §7 |

No compiler toolchain is needed. The dependency list is deliberately small and
every wheel is prebuilt.

---

## 2. Backend

```bash
git clone https://github.com/sameershinde6293/Automation-Studio.git
cd Automation-Studio/backend

python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt     # ~36 packages

./.venv/bin/alembic upgrade head                # creates the schema
./.venv/bin/uvicorn app.main:app --reload
```

Expect: 8 migrations applied, then the server listening on
**http://localhost:8000**.

> **Run `app.main:app`, never `main:app`.** `backend/main.py` is a 15-line V1.0
> stub retained only for an existing test; starting it serves an app with no
> routers and no database.

Verify:

```bash
curl http://localhost:8000/health         # {"status":"healthy"}
curl http://localhost:8000/health/ready   # checks DB, scheduler, workers, config
curl http://localhost:8000/metrics        # Prometheus exposition
```

`/docs` serves interactive API documentation in development.

---

## 3. Frontend

```bash
cd frontend
ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm install
npm run dev
```

Open **http://localhost:5173**.

`ELECTRON_SKIP_BINARY_DOWNLOAD=1` avoids a ~100 MB download that the browser app
never uses, and which fails outright behind TLS-inspecting proxies. Everything
except launching the desktop shell works without it — dev server, tests,
typecheck and production build.

For the desktop shell:

```bash
npm install            # without the skip flag, so Electron is downloaded
npm run electron:dev
```

> The Electron shell could not be launched during the M7 audit (the binary
> download is blocked in that environment). The browser app, tests, typecheck
> and production build were all verified.

---

## 4. Configuration

Optional for local use — the defaults are a working desktop configuration.

```bash
cp .env.example .env      # at the REPOSITORY ROOT, not inside backend/
```

The file is found whether you start the server from the repository root or from
`backend/`. Precedence, lowest first: `<repo>/.env` → `<repo>/backend/.env` →
`$PWD/.env` → real environment variables. Set `CREATOR_OS_ENV_FILE=/path/to/file`
to override the search entirely.

> Before v1.1.0-rc1 this did not work: `.env` was resolved relative to the
> working directory only, so a file at the repository root was silently ignored
> when starting from `backend/` — and the process fell back to *every* default,
> including authentication off. Fixed in M7 (M7-F1); see
> [M7_RELEASE_AUDIT.md](M7_RELEASE_AUDIT.md).

Settings worth knowing early:

```bash
DATABASE_URL=sqlite:///./creator_os.db   # or postgresql+psycopg://…
LOG_LEVEL=INFO
LOG_FORMAT=console                       # json for aggregation
OPENAI_API_KEY=                          # blank = fall back to Ollama, then mock
OLLAMA_BASE_URL=http://localhost:11434/api
MEDIA_ROOT=./media_storage
```

Every setting is documented in `.env.example` (development) and
`.env.production.example` (server). List values accept either
`A=one,two` or `A=["one","two"]`.

---

## 5. Verify the installation

```bash
# 1. examples — imports, runs and exports 4 workflows against your backend
python scripts/verify_examples.py

# 2. backend tests
cd backend && ./.venv/bin/python -m pytest -q

# 3. frontend tests, typecheck and production build
cd frontend && npm test && npm run typecheck && npx vite build

# or all of the above
./scripts/ci-local.sh
```

Expected on a healthy install:

```
4/4 examples passed
1484 passed, 8 skipped          # skips are PostgreSQL-gated by design
179 passed                      # frontend
```

---

## 6. PostgreSQL (server installs)

SQLite is single-writer and cannot back a multi-process deployment.

```bash
createdb creator_os
createuser creator --pwprompt
```

```bash
# .env
DATABASE_URL=postgresql+psycopg://creator:PASSWORD@localhost:5432/creator_os
```

```bash
cd backend && ./.venv/bin/alembic upgrade head      # creates 19 tables
```

The `psycopg[binary]` driver is already in `requirements.txt` — no libpq headers
or compiler needed.

Verified in M7 against PostgreSQL 16.2: full test suite (1492 passed, zero
skips), migrations, upgrade/downgrade/round-trip, production boot, backup and
restore. See [DEPLOYMENT.md](DEPLOYMENT.md) for the full server procedure and
[M7_RELEASE_AUDIT.md](M7_RELEASE_AUDIT.md) §4 for the evidence.

---

## 7. Docker

```bash
cp .env.production.example .env
# set AUTH_SECRET_KEY and POSTGRES_PASSWORD
docker compose --profile tools run --rm migrate
docker compose up -d
curl -fsS http://localhost:8080/health/ready
```

> **Unverified.** No container runtime has been available in M5, M6 or M7, so
> `docker build` and `docker compose up` have never been executed. The assets
> are statically validated by 23 tests (service topology, port and probe
> consistency, env-var contract, image hardening) and every process the
> containers would run has been verified outside them — but that is not the same
> as running the stack. See [M7_RELEASE_AUDIT.md](M7_RELEASE_AUDIT.md) §6.

---

## 8. Pre-built desktop binaries

```bash
cd frontend
npm install
npm run electron:build     # -> dist-electron-app/
```

Produces an AppImage on Linux, NSIS installer on Windows, and a `.dmg` on macOS.
**Not exercised in the M7 audit** — no release binaries are currently published.

---

## 9. Common installation problems

| Symptom | Cause and fix |
| --- | --- |
| `npm install` fails downloading Electron | TLS-inspecting proxy. `ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm install` |
| `no such table: workflows` | Migrations not applied. `alembic upgrade head` |
| API returns 404 for everything | You started `main:app`. Use `app.main:app` |
| `.env` appears to be ignored | Pre-rc1 build — upgrade, or set `CREATOR_OS_ENV_FILE` |
| `CERTIFICATE_VERIFY_FAILED` from HTTP nodes | Corporate TLS interception. Export `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` **for the backend process** (the HTTP node runs server-side) — setting it on a client script has no effect |
| Media nodes report FFmpeg missing | Install `ffmpeg`, or set `FFMPEG_BINARY`. Image handling works regardless |
| Port 8000 or 5173 already in use | `uvicorn --port 8001`, or `npm run dev -- --port 5174` |

Longer list with diagnosis steps: **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**.

---

## 10. Next steps

- [examples/README.md](../examples/README.md) — four workflows you can run now
- [USER_GUIDE.md](USER_GUIDE.md) — building your own
- [DEPLOYMENT.md](DEPLOYMENT.md) — running it as a server
- [SECURITY.md](SECURITY.md) — read before enabling script nodes
