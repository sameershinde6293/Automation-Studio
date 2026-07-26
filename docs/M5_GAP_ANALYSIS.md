# M5 Gap Analysis — Production Readiness & Platform Hardening

**Date:** 2026-07-26
**Baseline commit:** `bba4d08` (`main`, after PR #5 — M4 Execution Engine & AI Orchestration)
**Scope:** complete production-readiness audit of Creator OS v1.1 before any M5 code is written.

## 0. Verification performed before the audit

| Check | Result |
| --- | --- |
| Repository name is `Automation-Studio` | ✅ |
| Git remote is `https://github.com/sameershinde6293/Automation-Studio.git` | ✅ |
| PR #5 "M4: Execution Engine & AI Orchestration" merged into `main` | ✅ merged 2026-07-26T14:11:20Z |
| Latest `main` contains the M4 execution engine | ✅ `engine.py` 1793 LOC with `run_execution_v2`, `queue.py`, `streaming.py`, `control.py`, `history.py`, migration `c4e7a1b90d52` |
| M5 branch created from latest `main` | ✅ `arena/019f9f05-automation-studio` from `bba4d08` |

## 1. Measured baseline (not estimated)

| Metric | Value | How measured |
| --- | --- | --- |
| Backend Python LOC | 20,798 | `find backend -name '*.py' \| xargs wc -l` (excl. venv) |
| Frontend TS/TSX/CSS LOC | 3,827 | `find frontend/src -type f \( -name '*.ts' -o -name '*.tsx' -o -name '*.css' \) \| xargs wc -l` |
| Backend tests | 1,085 passed, 0 failed (27.8s) | `pytest -q` |
| Frontend tests | 105 passed, 0 failed (10 files) | `npm run test` |
| Frontend typecheck | clean | `tsc --noEmit` |
| Alembic migrations | 7, single linear head `c4e7a1b90d52` | inspected `down_revision` chain |
| Unused imports (F401/F841) | 6 | `ruff check app --select F401,F841` |

The suite is genuinely green. The gaps below are **absent capabilities**, not broken ones.

---

## 2. Architecture weaknesses

| # | Finding | Severity | Evidence |
| --- | --- | --- | --- |
| A1 | **No authentication layer exists anywhere.** There is no user model, no credential store, no token issuance, no session, no API key. Every one of the ~80 endpoints is anonymous. | **Critical** | `grep -rni "jwt\|oauth\|bearer\|authenticat" backend/app` returns only the OpenAI provider's outbound `Authorization` header and log-redaction regexes. |
| A2 | **RBAC is decorative.** `EnterpriseAuth.check_permissions` / `require_permission` exist and are unit-tested, but no router, service or dependency ever calls `require_permission`. The only consumer is `POST /api/enterprise/permissions/check`, which answers hypothetical questions. | **Critical** | `grep -rn "require_permission" backend/app` → definition only. |
| A3 | **Single-process execution.** The priority queue and worker pool live in process memory. Two backend replicas would each run independent queues over the same database and could double-execute the same row. | High | `queue.py:ExecutionQueue` uses `list` + `asyncio.Event`; no DB claim/lease. |
| A4 | **Process-wide engine write lock.** `WorkflowEngine._write_lock` serialises all node-status persistence across concurrent executions. | Medium | `engine.py`; documented in `KNOWN_ISSUES.md` #4. |
| A5 | **Dead legacy entrypoint.** `backend/main.py` is a 15-line FastAPI stub from V1.0 that shadows the real `backend/app/main.py`. Starting `uvicorn main:app` from `backend/` silently serves an app with no routers. | Medium | `backend/main.py`. |
| A6 | **Frontend does not mount its own product.** `App.tsx` renders the string "DAG Execution & Orchestration active." for the Workflows tab. The M3/M4 `WorkflowEditor`, canvas, palette and execution panel are never rendered by the running app — they are reachable only from tests. | **Critical (product)** | `App.tsx` has no import of `WorkflowEditor`. |
| A7 | Enterprise/Automation tabs are static `<ul>` marketing copy. | Medium | `Enterprise.tsx` (14 LOC), `Automation.tsx` (10 LOC). |
| A8 | In-memory-only state (AI traces, event bus ring buffer, execution log ring buffer, rate-limit buckets) is lost on restart and is not shared across processes. | Medium | `event_bus.py`, `streaming.py`, `middleware.py`. |

## 3. Security issues

| # | Finding | Severity |
| --- | --- | --- |
| S1 | No authentication on any endpoint (see A1). Anyone who can reach port 8000 has full control: create workflows, execute them, read audit logs, delete media. | **Critical** |
| S2 | No authorization enforcement (see A2). | **Critical** |
| S3 | `POST /api/enterprise/audit` accepts an arbitrary `user_id` and `event_name` from the client — the audit trail is forgeable and unauthenticated. | **Critical** |
| S4 | **Audit logging is not wired to anything.** No mutating endpoint writes an audit event. Deleting a workflow, enabling the shell executor, or running a workflow leaves no audit record. | High |
| S5 | `CORSMiddleware(allow_credentials=True)` combined with a permanently permissive default origin list (`file://`, `app://.`) and no production override check. | High |
| S6 | No CSRF defence. Currently latent (no cookies are issued), but becomes exploitable the moment cookie auth is added. | High (conditional) |
| S7 | No `TrustedHostMiddleware` → Host-header injection / DNS-rebinding against the local API from a browser. | High |
| S8 | Rate limiter keys on `request.client.host` only, ignores `X-Forwarded-For`, and is per-process. Behind any reverse proxy every caller collapses into one bucket (a single abusive client can DoS all users). | High |
| S9 | No startup validation: production can boot with `ENVIRONMENT=production`, Swagger enabled, permissive CORS, SQLite, rate limiting off, and dangerous executors on. Nothing warns or refuses. | High |
| S10 | Secret management is env-vars only; no support for secret files (`*_FILE`), and no check that secrets are absent from committed configs. | Medium |
| S11 | `SecurityHeadersMiddleware` sets a good CSP but no HSTS, and the CSP is identical in dev and prod. | Medium |
| S12 | The Electron shell has **no preload script, no CSP meta, no navigation/`window.open` guard**, and opens DevTools in dev unconditionally. `contextIsolation`/`sandbox` are correctly on. | Medium |
| S13 | Upload validation exists (magic-byte sniffing, name sanitisation, `MEDIA_ROOT` confinement, size cap) but the `/api/media/upload` path is exempted from the body-size middleware and there is no per-extension allow/deny list or archive-bomb guard. | Medium |
| S14 | SQL injection: **not exploitable** in application code — every query is SQLAlchemy ORM or `text()` with bound params. The `database` node exposes raw SQL by design, is flag-gated off, rejects stacked statements and gates writes. Verified, no action beyond documentation. | Info |
| S15 | XSS: React escapes by default and no `dangerouslySetInnerHTML` exists in the codebase. Verified clean. | Info |

## 4. Script sandbox

| # | Finding | Severity |
| --- | --- | --- |
| X1 | The Python node is a **restricted `exec`**, not a sandbox. Blocklist regexes (`import`, `__dunder__`, `open(`) are string-level and bypassable; a restricted-builtins `exec` is a well-known escapable boundary. Honest docs already say so. | **Critical if enabled** |
| X2 | No **CPU-time limit** — only a wall-clock `asyncio.wait_for`. A busy loop burns a core for the full timeout and the thread is never actually killed (`asyncio.to_thread` cannot cancel a running thread), so a `while True:` leaks a CPU-pinned thread for the process lifetime. | **Critical if enabled** |
| X3 | No **memory limit**. `[0]*10**10` OOM-kills the whole backend. | **Critical if enabled** |
| X4 | No **filesystem isolation** — the JavaScript node runs with the full permissions of the backend OS user and can read `.env`/the SQLite DB. | **Critical if enabled** |
| X5 | No **network restriction** for the JS node (`fetch`/`net` fully available). | High if enabled |
| X6 | No **execution quotas** — unlimited script-node invocations per execution/workflow. | Medium |
| X7 | No output-size limit on script stdout beyond a 64 KB slice after the fact (the buffer itself is unbounded during the run). | Medium |

Mitigating factor: all three risky executors ship **disabled by default** and are honestly documented.

## 5. Scalability limits

| # | Finding | Severity |
| --- | --- | --- |
| C1 | Horizontal scaling is impossible: in-memory queue, in-memory rate limiter, in-memory SSE broker, in-memory AI traces (A3, A8). | High |
| C2 | SQLite is the default and only exercised backend. WAL + pragmas are tuned, but writer concurrency is still one. Postgres is configured for in `_engine_kwargs()` but never tested. | High |
| C3 | SSE fan-out is per-process; a client connected to replica A sees nothing from replica B. | Medium |
| C4 | No connection-pool metrics or saturation signal. | Medium |
| C5 | List endpoints return bare arrays with `skip`/`limit` but **no total count**, so a client cannot page deterministically or show "page 3 of 40". | Medium |

## 6. Missing production features

| # | Gap |
| --- | --- |
| P1 | **No Dockerfile, no docker-compose, no container assets of any kind.** |
| P2 | No production `.env` template (`.env.example` is development-shaped and omits every M4 setting). |
| P3 | No startup configuration validation / fail-fast. |
| P4 | No Prometheus/OpenMetrics endpoint. `/api/system/metrics` returns bespoke JSON that no standard scraper understands. |
| P5 | No error aggregation — exceptions are logged and lost; there is no "top errors in the last hour" surface. |
| P6 | No graceful-shutdown drain semantics for in-flight executions (shutdown cancels after a 5s timeout). |
| P7 | No API versioning. Everything is under `/api` with no `/api/v1` alias, so no path to a breaking v2. |
| P8 | No `SECURITY.md`, `DEPLOYMENT.md`, `ARCHITECTURE.md` or `CONTRIBUTING.md`. |
| P9 | Frontend has no error boundary, no notification/toast system, and no retry affordance on failed fetches. |
| P10 | Health endpoints exist (`/health`, `/health/live`, `/health/ready`) and are good — readiness checks the DB and scheduler. Gap: readiness ignores the worker pool and the queue. |

## 7. Testing gaps

| # | Gap |
| --- | --- |
| T1 | **Zero security tests** — nothing asserts that an unauthenticated request is rejected, because nothing rejects it. |
| T2 | No authentication/authorization tests (the capability does not exist). |
| T3 | **No migration tests.** Alembic upgrade/downgrade has never been executed in CI. The `audit_events` table is created by `create_all()` and has **no migration at all** — a migration-only deployment starts with a missing table and `POST /api/enterprise/audit` fails at runtime. |
| T4 | No performance smoke tests (no assertion on API latency, engine throughput or startup time). |
| T5 | No frontend accessibility tests; no error-boundary tests; no frontend performance tests. |
| T6 | No browser E2E infrastructure (no Playwright/Cypress). Two Python HTTP smoke scripts exist in `scripts/` but are not run by CI. |
| T7 | Backend coverage has not been measured since M2 (the "94%" in `PROJECT_STATUS.md` is stale and was carried forward through M3 and M4). |
| T8 | CI (`ci/github-actions-ci.yml`) is a file in `ci/`, **not** in `.github/workflows/`, so it has never run. It also calls `npm run test:run`, a script that does not exist in `package.json` (the real one is `test`), so the frontend test step is a silent no-op even if activated. |

## 8. Deployment gaps

| # | Gap |
| --- | --- |
| D1 | No container images (P1). |
| D2 | No orchestration/compose file, no reverse-proxy example, no TLS guidance. |
| D3 | No documented migration step for deploys (`alembic upgrade head` appears nowhere in the docs). |
| D4 | `INSTALLATION_GUIDE.md` is 19 lines and development-only. |
| D5 | No non-root container user, no healthcheck definition, no resource limits. |
| D6 | CI is inactive (T8) and there is no release/build pipeline. |

## 9. Documentation gaps

| # | Gap |
| --- | --- |
| G1 | Missing `SECURITY.md`, `DEPLOYMENT.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`. |
| G2 | `PROJECT_STATUS.md` reports a stale coverage number and an M5 scope ("Advanced Media Pipeline UX") that no longer matches this milestone. |
| G3 | `docs/TODO.md` still says "Version 1.0 is feature-complete" and lists a visual node editor as future work — it shipped in M3. |
| G4 | `API_DOCUMENTATION.md` is a hand-maintained endpoint list with no request/response schemas, no auth section, no rate-limit/pagination contract. |
| G5 | `docs/RELEASE_NOTES.md` is 4 lines; a duplicate `release_notes.txt` sits in the repo root. |
| G6 | No documented operational runbook (backup, restore, log locations, rotation). |

## 10. Technical debt

| # | Item |
| --- | --- |
| Q1 | Dead `backend/main.py` V1.0 stub (A5). |
| Q2 | 6 unused imports (`ruff F401`) in `ai/models.py`, `media_repository.py`, `settings.py`, `ffmpeg.py`, `storage.py`. |
| Q3 | `backend/test_endpoints.sh` is a manual curl script referencing a `venv/` layout that `.gitignore` excludes; superseded by the pytest suite. |
| Q4 | Root `release_notes.txt` duplicates `docs/RELEASE_NOTES.md`. |
| Q5 | `media_router.py` has a private `_as_dict` serialiser used once, and mixes `202` responses via two different code paths in `upload_asset`. |
| Q6 | No linter is configured for the backend (`ruff` is not in `requirements.txt`); `frontend/.oxlintrc.json` exists but no lint script invokes it. |
| Q7 | `WORKFLOW_MAX_NODES` is defined in settings but never enforced. |

## 11. Performance bottlenecks

| # | Finding | Severity |
| --- | --- | --- |
| F1 | Global engine write lock serialises node persistence (A4). | Medium |
| F2 | `GET /api/executions` search does `count()` + `all()` and serialises full rows; no covering index for the `search` LIKE path. | Medium |
| F3 | Every request allocates a new SQLAlchemy session even for endpoints that never touch the DB (`/api/system/*`). | Low |
| F4 | `RateLimitMiddleware` sweeps up to 1,024 buckets inline on a request thread. | Low |
| F5 | Frontend: `WorkflowCanvas` rebinds a `keydown` listener on every `rfNodes` change; the store's history array is unbounded (memory growth on long editing sessions). | Medium |
| F6 | Frontend has no code splitting — one bundle including React Flow. | Low |
| F7 | Startup imports the entire node library eagerly; measured startup is fast today but unmeasured and ungated. | Low |
| F8 | No latency instrumentation beyond the `X-Response-Time-ms` header; no percentiles. | Medium |

---

## 12. Honest baseline assessment

Creator OS at `bba4d08` is a **functional single-user local application**, not a platform. The engine, node runtime, AI orchestration and editor internals are genuinely good and genuinely tested (1,085 backend + 105 frontend tests, all green). What is missing is everything that makes software safe to expose to more than one trusted person on one machine:

- it has **no identity**, so it can have no authorisation, no attribution and no trustworthy audit trail;
- it **cannot be deployed** by a documented, reproducible process;
- it **cannot be observed** by standard tooling;
- its **CI has never executed**;
- its **migrations have never been tested**, and one table has no migration at all.

**Production readiness at baseline: ~35%.** The application layer is well past halfway; the platform layer is close to zero.

## 13. M5 priority order (what this milestone will actually change)

1. **P0 — Identity & access:** principal model, token/API-key authentication, RBAC enforcement dependencies applied per endpoint, forgery-proof audit logging wired into mutating routes, CSRF for cookie flows, trusted hosts, production CORS validation.
2. **P0 — Startup validation:** refuse to boot an unsafe production configuration.
3. **P0 — Migration correctness:** `audit_events` migration + new tables/indexes, and a real upgrade/downgrade test in CI.
4. **P1 — Sandbox hardening:** move script execution into a subprocess with POSIX `RLIMIT_CPU`/`RLIMIT_AS`/`RLIMIT_FSIZE`/`RLIMIT_NPROC`, kill-on-timeout, import allow-listing, temp-dir-only filesystem, network opt-out, and per-execution quotas — with an honest statement of what this still does **not** guarantee.
5. **P1 — Observability:** Prometheus exposition, request/execution metrics, error aggregation, richer readiness.
6. **P1 — Deployment:** backend + frontend Dockerfiles, compose stack, production env template, deployment docs, activate CI.
7. **P2 — API consistency:** pagination envelope with totals, `/api/v1` versioning alias, uniform error/response contract.
8. **P2 — Frontend hardening:** mount the real editor, error boundary, toasts, loading/retry UX, a11y and keyboard navigation, listener/state cleanup, Electron preload + CSP + navigation guards.
9. **P2 — Tests:** security, authz, migration, performance smoke, a11y, error boundary; E2E infrastructure.
10. **P3 — Code quality & docs:** remove dead code and unused imports, add linting, write the four missing documents, correct the stale ones.

Anything not completed will be listed, unmitigated, in the final report and in `KNOWN_ISSUES.md`.
