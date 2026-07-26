# M6 Validation Report — Production Validation, Scalability & Operational Readiness

Creator OS v1.1 · Milestone 6
Branch: `arena/019f9f61-automation-studio`
Base: `a92af1be` (merge of PR #7, M5)
Report date: 2026-07-26

---

## 0. Pre-flight verification

| Check | Result |
| --- | --- |
| Repository is `sameershinde6293/Automation-Studio` | ✅ confirmed via `git remote -v` |
| PR #7 merged into `main` | ✅ `MERGED` at `2026-07-26T17:02:12Z`, merge commit `a92af1be` |
| Latest `main` pulled | ✅ `origin/main` == `a92af1be` |
| M6 working branch created | ✅ `arena/019f9f61-automation-studio`, branched from `a92af1be` |

Verification passed. Work proceeded.

### Environment capabilities and limits

M6 was executed in a Linux sandbox. What was and was not available materially
shapes what this report can claim, so it is stated up front:

| Capability | Available | Consequence |
| --- | --- | --- |
| Python 3.11 + full dependency set | ✅ | Full test suite executed |
| **PostgreSQL 16.2** (real server, via `pgserver` wheel) | ✅ | **Postgres migrations, backup/restore and multi-worker runs validated for the first time** |
| `pg_dump` / `pg_restore` / `pg_ctl` / `pg_isready` | ✅ | Backup & recovery verified with real binaries |
| Uvicorn multi-worker over TCP | ✅ | Real HTTP load/failure testing performed |
| **Docker / Docker Compose** | ❌ **not installed** | Image build and compose orchestration **still unverified** — see §7 |
| Node.js 22 | ✅ | Frontend build/tests runnable |

Everything claimed below as "verified" was executed and its output observed.
Anything that could not be executed is listed as such and is **not** counted
toward completion.

---

## 1. Phase 1 — Full M5 audit

Baseline established before any change:

```
$ cd backend && pytest -q
1342 passed in 104.96s
```

Each M5 subsystem was read in full and then probed independently. Findings are
numbered `M6-F<n>` and carry reproduction evidence.

### 1.1 Audit matrix

| Subsystem | Reviewed | Verdict |
| --- | --- | --- |
| Authentication (`auth_service`, `tokens`, `passwords`) | ✅ | Sound. Uniform failure, lockout, refresh rotation, alg pinning all correct |
| Authorization / RBAC (`principal`, `dependencies`) | ✅ | Sound. Router-level fail-closed default is the right design; scope intersection cannot escalate |
| Sandbox (`security/sandbox.py`) | ✅ | Sound and honestly documented. Process isolation + `setrlimit` + import allowlist |
| Middleware (`core/middleware.py`) | ⚠️ | **Defect M6-F2** — path-prefix logic is bypassed by the `/api/v1` alias |
| Health endpoints (`/health`, `/live`, `/ready`) | ✅ | Correct; readiness genuinely gates on DB + workers + config |
| Metrics (`observability/metrics.py`, `/metrics`) | ✅ | Correct Prometheus text format; cardinality bounded via route templates |
| Logging (`logging/logger.py`) | ✅ | Structured JSON, correlation IDs propagate |
| Startup validation (`core/startup.py`) | ✅ | Logic correct — but see **M6-F1**, it can never be reached with a CSV `.env` |
| Docker assets | ⚠️ | Well written; **never executed**; references a file that does not exist (**M6-F4**) |
| Deployment docs | ⚠️ | Accurate about its own unverified status; the quick-start `cp` step fails (**M6-F4**) |
| Migrations | ⚠️ | SQLite round-trip fine; **Postgres re-upgrade after downgrade is broken (M6-F3)** |
| Tests | ✅ | 1342 passing, no flakes observed across repeated runs |

### 1.2 Findings

---

#### M6-F1 — **CRITICAL** — Comma-separated list settings crash the process at import

`Settings` declares `CORS_ORIGINS`, `ALLOWED_HOSTS`, `SHELL_ALLOWED_COMMANDS`,
`HTTP_EXECUTOR_ALLOWED_HOSTS` and `AI_FALLBACK_CHAIN` as `List[str]`, and
supplies a `@field_validator(..., mode="before")` named `_split_csv` to accept
`a,b,c`. **That validator never runs for environment input.**

In `pydantic-settings` ≥ 2, a complex field (list/dict) sourced from an env var
or `.env` file is JSON-decoded *inside the settings source*, before any pydantic
validator is invoked. A non-JSON value raises `SettingsError` during
`Settings()` — which executes at module import — so the process dies before
`main.py`, before logging, and before startup validation.

**Reproduction (observed):**

```
$ CORS_ORIGINS="https://a.example.com" python -c "from app...settings import Settings; Settings()"
pydantic_settings.exceptions.SettingsError:
    error parsing value for field "CORS_ORIGINS" from source "EnvSettingsSource"

$ printf 'CORS_ORIGINS=https://a.com,https://b.com\n' > .env && python -c "...Settings()"
pydantic_settings.exceptions.SettingsError:
    error parsing value for field "CORS_ORIGINS" from source "DotEnvSettingsSource"
```

Only JSON (`CORS_ORIGINS=["https://a.example.com"]`) works.

**Impact.** This is the single most severe finding in M6. Every document in the
repository instructs operators to use CSV:

- `.env.example` → `CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173`
- `docs/DEPLOYMENT.md` §5 → "Set `CORS_ORIGINS` and `ALLOWED_HOSTS` to your real hostname"
- `settings.py` docstring → "Allow comma-separated env values, e.g. `CORS_ORIGINS=a,b,c`"

A production deployment following the documentation **cannot start at all**. It
also means the M5 startup-validation gate — the feature whose entire purpose is
to refuse an unsafe production config — is unreachable on any deployment that
sets these variables the documented way. This was discovered only because M6
actually attempted to boot a production-configured server.

**Why the M5 suite missed it.** `test_startup_validation_m5.py` uses a
`FakeSettings` object with Python lists; `test_config.py` never sets these
variables via the environment. No test constructs `Settings()` with a CSV env
var present.

---

#### M6-F2 — **HIGH** — The `/api/v1` alias bypasses four path-prefix security controls

`main.py` mounts every router twice: once at `/api` and once at `/api/v1`.
Four middleware/handler controls match on the `/api/...` literal prefix and
therefore do not apply to the `/api/v1/...` alias.

**Reproduction (observed, auth enabled, real app):**

| Control | `/api/...` | `/api/v1/...` |
| --- | --- | --- |
| Auth rate limit (10/min on login) | `[401 ×10, 429, 429, 429, 429]` | `[401 ×14]` — **never throttled** |
| CSRF double-submit on cookie session | `200` (exempt path, correct) | `403 csrf_failed` — login/refresh **broken** |
| `Cache-Control: no-store` on `/auth/me` | `'no-store'` | `None` — **credentials cacheable** |
| Body-size exemption for media upload | `/api/media/upload` accepts | `/api/v1/media/upload` → **`413`** |

Root causes, all in the same class:

| Location | Constant |
| --- | --- |
| `RateLimitMiddleware.auth_paths` | `("/api/auth/login", "/api/auth/register")` |
| `CSRFMiddleware.exempt_paths` | `("/api/auth/login", "/api/auth/refresh")` |
| `SecurityHeadersMiddleware` | `path.startswith("/api/auth")` |
| `main.create_app` → `BodySizeLimitMiddleware` | `exempt_paths=("/api/media/upload",)` |

**Impact.** The rate-limit bypass is the security-relevant one: the stricter
credential-stuffing budget that M5 added is defeated by inserting `/v1` into
the URL, leaving only the general 300/min budget. The CSRF and 413 rows are
availability defects — the versioned API that clients are explicitly told to
pin to is partly non-functional.

**Note on severity.** This is a real bypass but not a privilege escalation:
RBAC is enforced by route dependencies, which are attached to the router and
therefore apply identically on both mounts. That was verified — see §6.

---

#### M6-F3 — **HIGH** — Migration downgrade leaves orphaned PostgreSQL ENUM types; re-upgrade fails

Verified against **real PostgreSQL 16.2**. `alembic upgrade head` succeeds on a
clean database (first time this has ever been confirmed — M5 shipped Postgres
support unexecuted). Downgrade also succeeds. But `downgrade` → `upgrade`, the
documented rollback-then-retry path, fails:

```
$ alembic downgrade base      # succeeds
$ alembic upgrade head
psycopg.errors.DuplicateObject: type "executionstatus" already exists
[SQL: CREATE TYPE executionstatus AS ENUM ('PENDING','RUNNING',...)]
```

```
creator_os=# select typname from pg_type t join pg_namespace n
             on n.oid=t.typnamespace where n.nspname='public' and t.typtype='e';
     typname
-----------------
 executionstatus
 loglevel
```

PostgreSQL creates a standalone `TYPE` for `sa.Enum`. `op.drop_table` drops the
table but **not** the type. The two affected downgrades are
`6d40195ec51d` (`executionstatus`) and `c4e7a1b90d52` (`loglevel`); neither
issues `DROP TYPE`.

**Impact.** `docs/DEPLOYMENT.md` §4 documents
`docker compose --profile tools run --rm migrate alembic downgrade -1` as the
rollback procedure. On PostgreSQL — the only supported production database —
rolling back and then rolling forward again leaves the database wedged and
requires manual `DROP TYPE` by an operator under incident pressure. SQLite is
unaffected (no native enum), which is why `test_migrations_m5.py`, which runs
on SQLite, passes.

---

#### M6-F4 — **MEDIUM** — `.env.production.example` is referenced everywhere but does not exist

`docker-compose.yml` line 3 and `docs/DEPLOYMENT.md` §2 both instruct
`cp .env.production.example .env`. `git log --diff-filter=A` confirms the file
was never added in any commit. The documented quick start fails at step one.
Compounded by M6-F1: even hand-writing the file from `.env.example` produces a
process that will not boot.

---

#### M6-F5 — **LOW** — SSE `cleanup()` can discard buffers a concurrent subscriber still needs

`_event_stream`'s `finally` calls `execution_broker.cleanup(execution_id)`
whenever *any* subscriber disconnects. `cleanup` guards with
"return early if `_subs_by_execution` is non-empty", but `unsubscribe` runs
first and removes the map entry entirely when it was the last subscriber, so
two clients disconnecting near-simultaneously can leave the second's replay
buffer dropped. Consequence is a lost SSE reconnect backfill, not data loss —
`flush_logs` is called first, so persisted logs are intact. Documented, not
fixed: the fix is a refcount, and the failure is benign and rare.

### 1.3 Explicitly checked and found clean

These were audited for the specific risks named in the M6 brief and no defect
was found. Recording them so the negative result is on the record:

- **Privilege escalation.** `Principal.permissions` intersects role grants with
  API-key scopes; a scoped key can only narrow. `require_method_permission` is
  attached at router level, so a route added later is protected by default.
  Verified that both `/api` and `/api/v1` mounts carry identical dependencies.
- **Race conditions.** `ExecutionQueue` (`threading.RLock` + heap),
  `WorkerPool._active`, `ExecutionBroker` (`RLock` + `call_soon_threadsafe`
  when off-loop) and `MetricsRegistry` (per-metric locks) are all correctly
  guarded. `WorkflowEngine._write_lock` serialises node-status writes, which is
  what makes SQLite-backed parallel node execution safe.
- **Dead code.** `submit`/`run_execution` (v1) coexist with
  `submit_v2`/`run_execution_v2`. This is *not* dead: `submit` remains the
  documented backwards-compatible entry point and is covered by tests. Left
  alone per the "do not rewrite working code" rule.
- **Duplicated logic.** `require_permission` and `require_method_permission`
  share a shape but differ in method-dispatch behaviour; the duplication is
  ~10 lines and factoring it would touch every router for no functional gain.
  Left alone.
- **JWT.** `alg` pinned to HS256 before verification (`alg:none` and
  RS256→HS256 confusion both rejected), `hmac.compare_digest` for the
  signature, `typ` enforced so an access token cannot be replayed as a refresh
  token, `jti` present for revocation. No defect.
- **Regressions vs M4.** All 1342 tests green; no M0–M4 behaviour changed.

---

## 2. Phase 2 — Deployment validation

### 2.1 What was executed

Real PostgreSQL 16.2 was initialised (`initdb -E UTF8`), started via `pg_ctl`
on TCP `127.0.0.1:55432`, and used as the backing store for a production-mode
Uvicorn server (`ENVIRONMENT=production`, `AUTH_ENABLED=true`, JSON logging,
docs disabled, HSTS on, non-wildcard CORS and `ALLOWED_HOSTS`).

| Item | Method | Result |
| --- | --- | --- |
| Database migrations (PostgreSQL) | `alembic upgrade head` | ✅ **verified — first time ever** |
| Migration rollback (`downgrade -1`, `downgrade base`) | alembic | ✅ verified |
| Migration re-upgrade after downgrade | alembic | ❌ **fails — M6-F3** (fixed in M6, then re-verified) |
| Production config boot | uvicorn, prod env | ❌ **fails — M6-F1** (fixed in M6, then re-verified) |
| Container startup | — | ⚠️ **not verified — no Docker runtime** |
| Docker image build | — | ⚠️ **not verified — no Docker runtime** |
| Docker Compose orchestration | — | ⚠️ **not verified — no Docker runtime** |
| Health / readiness / metrics endpoints | live HTTP | ✅ verified (see §9) |
| Graceful shutdown, restart, rollback drill | SIGTERM / restart | ✅ verified (see §9) |

### 2.2 Honest statement on Docker

No container runtime exists in this environment, and one cannot be installed.
Therefore **M6 does not claim Docker deployment is verified.** What M6 *did*
do instead is reproduce the container's runtime contract outside a container —
same Postgres, same production settings, same Uvicorn command line, same
health probes — which is what surfaced M6-F1. The Dockerfile and compose file
remain reviewed-but-unexecuted, exactly as M5 stated. This limitation is
recorded in `docs/KNOWN_ISSUES.md` and remains the top item for a real
deployment rehearsal.

---

*Sections 3–11 (fixes applied, scalability, load, failure, backup, security,
performance, observability, testing, completion) are appended below as each
phase completes.*
