# FAQ

Creator OS v1.1.0-rc1

## General

**What is Creator OS?**
A visual workflow automation platform with a built-in AI runtime. You build
directed graphs of AI calls, HTTP requests, media processing and scripted logic
in a drag-and-drop editor, then run them with retries, branching, loops, live
streaming and full execution history.

**Desktop app or server?**
Both, from one codebase. Local desktop: SQLite, no auth, zero configuration.
Server: PostgreSQL, JWT auth, RBAC, metrics. The difference is configuration,
not a different build.

**Is it production-ready?**
It is a **Release Candidate at 88% readiness**. The source and PostgreSQL paths
are verified end to end with evidence. The **Docker path has never been
executed** because no container runtime has been available in three consecutive
milestones. Single-process limits also apply — see below. Full accounting:
[M7_RELEASE_AUDIT.md](M7_RELEASE_AUDIT.md).

**Can I run more than one backend replica?**
Not yet. The execution queue, rate limiter and SSE broker all hold state in
process memory, so two replicas can double-execute the same queued run. Keep
`WEB_CONCURRENCY=1` and one replica; scale vertically. Details:
[DEPLOYMENT.md](DEPLOYMENT.md) §9.

**What licence is it under?**
There is currently **no licence file**, so by default all rights are reserved by
the repository owner. If you intend to use, fork or distribute it, ask the owner
to add an explicit licence first.

---

## Installation

**What do I actually need?**
Python 3.11+ and Node 22+. That is all. PostgreSQL, Docker, FFmpeg and Ollama
are optional, and the app runs without every one of them.

**Do I need an OpenAI key?**
No. Providers are tried in order (`AI_FALLBACK_CHAIN`, default
`openai → local → mock`). With nothing configured, the chain ends at the
built-in `mock` provider, which returns deterministic text — AI workflows still
execute. Example 02 is verified running exactly this way.

**Why `ELECTRON_SKIP_BINARY_DOWNLOAD=1`?**
It skips a ~100 MB download the browser app never uses, and which fails behind
TLS-inspecting proxies. Dev server, tests, typecheck and production build all
work without it; only launching the desktop shell needs it.

**Why does `npm install` warn about vulnerabilities?**
They come from dev-only transitive dependencies (build tooling). No runtime
dependency of the shipped bundle is affected.

**Where does `.env` go?**
The repository root. It is also found in `backend/`, or wherever you start the
process. Search order, lowest precedence first: `<repo>/.env` →
`<repo>/backend/.env` → `$PWD/.env` → real environment variables. Override with
`CREATOR_OS_ENV_FILE`.

> Before v1.1.0-rc1 a root `.env` was **silently ignored** when starting from
> `backend/`, and the app fell back to every default including authentication
> off. Fixed in M7 (M7-F1).

---

## Usage

**Why does my template render empty?**
Variables seeded by the `start` node are reached through the node:
`{{ Start.variables.my_var }}`, not `{{ my_var }}`. An empty render is not an
error, so nothing warns. Inspect `state.variables` and each node's `output_data`
in the execution detail.

**How do I schedule a workflow?**
There is no inbound trigger node. Call the API from cron, a systemd timer, or
any scheduler you already run:

```bash
curl -X POST localhost:8000/api/workflows/4/executions \
  -H 'Content-Type: application/json' -d '{"queued":true}'
```

See [examples/README.md](../examples/README.md) §Scheduling.

**`wait` vs `queued`?**
`{"wait":true}` runs synchronously and returns the result — good for cron, where
you want a non-zero exit on failure. `{"queued":true}` returns immediately and
runs through the priority queue. Queued runs do **not** survive a restart.

**Can I import and export workflows?**
Yes. `GET /api/workflows/{id}/graph` exports; `PUT` on the same path imports.
The editor has Import/Export buttons. Round-tripping is asserted on every
example by `scripts/verify_examples.py`.

**How do I see what a running workflow is doing?**

```bash
curl -N localhost:8000/api/executions/<id>/stream    # live SSE
curl -s localhost:8000/api/executions/<id>/logs      # persisted logs
```

**What happens when a node fails?**
It retries per its `retry_policy`, then fails the execution. To handle failure
instead, branch on the outcome with a `condition` node — see example 03.

**Are there really 22 node types?**
22 in the editor palette; the backend registers 73 names including aliases
(`http`, `httpRequest` and `http_request` are one executor). `GET
/api/system/node-types` lists the canonical catalogue.

---

## Security

**Is the Python sandbox safe for untrusted code?**
**No.** It is defence in depth — a separate OS process with kernel CPU and
memory limits and a PEP 578 audit hook — but a CPython escape yields the backend
user's privileges. The JavaScript node is not sandboxed at all. Both are
disabled by default. Read [SECURITY.md](SECURITY.md) §5 before enabling either.

**Why is my HTTP node refusing an internal URL?**
SSRF protection blocks private, loopback, link-local and cloud-metadata
addresses. Prefer an allowlist (`HTTP_EXECUTOR_ALLOWED_HOSTS`) over
`HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS=true`.

**Are secrets written to logs?**
No. A redacting filter masks API keys, bearer tokens and passwords before
anything is written. Verified in M7: the admin password and database password
appear **zero times** in the production log file.

**Can I revoke an access token?**
Not before it expires (default 15 minutes). Only refresh sessions are stateful.
Deactivating a user kills refresh but not a live access token.

**Is RBAC per-workflow?**
No, it is global. Any `editor` can modify any workflow. Do not use one instance
to isolate mutually untrusting users.

---

## Operations

**Does it work with PostgreSQL?**
Yes, and that is the only supported server database. Verified in M7 against
PostgreSQL 16.2: the full suite (1446 passed, zero skips), migrations,
upgrade/downgrade/round-trip, production boot, backup and restore.

**Is Docker verified?**
**No.** The images and compose stack have never been executed — no container
runtime in M5, M6 or M7. The assets are statically validated by 23 tests and
every process the containers would run has been verified outside them, but that
is not the same as running the stack. Treat your first containerised deployment
as a validation exercise.

**How do I back up?**

```bash
pg_dump -U creator creator_os | gzip > backup-$(date +%F).sql.gz
tar czf media-$(date +%F).tar.gz -C "$MEDIA_ROOT" .
```

Back up `.env` separately and securely. Restore was rehearsed in M7 and
recovered every row.

**Are there metrics?**
Prometheus exposition at `/metrics`:
`creator_os_http_requests_total`,
`creator_os_http_request_duration_seconds`,
`creator_os_executions_total`,
`creator_os_execution_queue_depth`,
`creator_os_auth_attempts_total`.

**Does it rotate logs?**
Set `LOG_FILE` for a rotating handler at 10 MB × 5 backups. Containers should
leave it unset and log JSON to stdout. (M7 note: the handler is configured
correctly, but a rollover was not triggered — that needs 10 MB of output.)

**How do I upgrade?**
Back up, `git pull`, reinstall dependencies, `alembic upgrade head`, restart.
Full procedure and rollback: [UPGRADE_GUIDE.md](UPGRADE_GUIDE.md).

**Is there CI?**
A pipeline exists at `ci/github-actions-ci.yml` but has **never run** — GitHub
only executes workflows from `.github/workflows/`, and the automation account
cannot create files there. A maintainer must copy it in. `./scripts/ci-local.sh`
runs the same checks locally.

---

## Development

**How do I run the tests?**

```bash
cd backend && ./.venv/bin/python -m pytest -q     # 1487 passed
cd frontend && npm test                           # 179 passed
python scripts/verify_examples.py                 # 4/4 examples
./scripts/ci-local.sh                             # everything
```

**Why do 8 tests skip?**
They need a real PostgreSQL. Set `TEST_POSTGRES_URL` to run them; they pass.

**What is `backend/main.py`?**
A 15-line V1.0 stub that shadows the real `app/main.py`. It is still imported by
`tests/test_main.py`, so it is not dead code and was not removed. Never start it
— it serves an app with no routers.

**How do I add a node type?**
Subclass `RuntimeNodeExecutor` in `app/services/workflow/nodes/`, declare a
`NodeSchema`, register it in `register_all`, then add the matching React
component in `frontend/src/components/nodes/`. See
[DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md).
