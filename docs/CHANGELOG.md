# Changelog

## [1.1.0-rc3] - 2026-07-28 — Release Candidate 3

### Milestone 9 — Production staging and real-world validation

No new features. M9 deployed Creator OS to a production-shaped staging
environment running **real PostgreSQL 16.2**, measured its behaviour, broke it
on purpose, and fixed only what the evidence showed to be broken. Full
evidence in `M9_VALIDATION_REPORT.md`.

The PostgreSQL server itself was the unlock: earlier milestones concluded no
PostgreSQL was available because Debian mirrors are unreachable, but PyPI is
reachable and the `pgserver` wheel bundles a complete PostgreSQL 16.2
distribution. That let the whole system — migrations, auth, execution, failure
injection, backup and restore — run against the same major version the compose
file pins, and it un-skipped 8 migration tests that had been skipped since M6.

#### Fixed

- **`backup.sh` reported success while producing no database backup
  (M9-F3, CRITICAL).** Executed against the PostgreSQL staging deployment, the
  script exited `0` after writing only `env.sanitized`, `manifest.txt` and an
  empty `media.tar.gz`. Four causes: a missing `pg_dump` was a warning rather
  than an error; a failing `pg_dump` was swallowed by `|| echo`; the connection
  was rebuilt from `POSTGRES_USER`/`POSTGRES_DB`, discarding the host and port
  from `DATABASE_URL`; and the media archive ignored `MEDIA_ROOT`. A cron job
  would have reported success indefinitely, with the failure surfacing only
  during a restore. Both scripts now fail loudly, read `DATABASE_URL` (with the
  SQLAlchemy `+psycopg` suffix translated for libpq and the password redacted
  in output), fall back to a bundled `pg_dump`/`psql`, verify the archive with
  `gunzip -t` and a minimum-size check, record SHA-256 of every artefact, and
  treat a backup with no database archive as a hard error. `restore.sh` now
  uses `ON_ERROR_STOP=1`, so a partially applied dump is a failure instead of a
  silent success. Verified by a full disaster-recovery drill: dump → drop →
  restore → 20 tables, all rows, `alembic_version` preserved, application boots
  against the restored database and authenticates with the restored credentials.

- **Database pool saturation was invisible in `/metrics` (M9-F1, HIGH).** Pool
  capacity is documented as the cap on request concurrency, and M6 measured it,
  but no pool metric was ever exported. Pool exhaustion presents as requests
  blocking on checkout, which from outside is indistinguishable from a slow
  database. Added `creator_os_db_pool_size`, `_checked_out`, `_available`,
  `_overflow`, `_capacity` and `_utilisation_ratio`, refreshed on scrape and
  wrapped so a broken pool can never break a scrape. Verified live: 40
  concurrent clients drove `checked_out` to 40 and the ratio to 0.5.

- **Account lockout produced no audit event (M9-F2, MEDIUM).** After 25 rapid
  bad logins the account locked correctly, but the audit table contained only
  `auth.login.failed` rows — the lockout existed solely as a log line, so an
  audit-trail consumer or SIEM export could not see that an account had been
  locked. `auth.account.locked` is now emitted after the lockout commits, with
  username, failure count, `locked_until` and the configured window. Auditing
  stays best-effort: a broken audit sink must not deny logins.

- **Shipped version disagreed with published version (M9-F4).** README and
  `PROJECT_STATUS.md` advertised `1.1.0-rc2` while the backend, frontend and
  live `/health/ready` all reported `1.1.0-rc1`. Everything is now
  `1.1.0-rc3`, and a regression test requires backend, settings,
  `package.json`, `package-lock.json`, README, `PROJECT_STATUS.md` and the
  running health payload to agree.

- **README test counts were stale (M9-F5).** Quoted 1529 passed / 8 skipped;
  the suite produced 1527 / 10. Now measured and linked to the report that
  produced the numbers.

#### Validated (executed, not asserted)

- **Staging on real PostgreSQL 16.2** — production settings, auth on, docs off,
  JSON logs, migrations applied, bootstrap admin created then cleared.
- **Long run** — 48 minutes continuous under load: 408 workflow executions,
  0 failures, 0 non-200 probes, RSS 91→100 MB, CPU 1.0% of one core, FDs and
  threads bounded, no `idle in transaction` leak.
- **Performance** — `/health` p95 2.9 ms; workflow execution p95 68 ms;
  100 concurrent readers with 0 errors; startup 1.1 s; graceful shutdown 188 ms
  (5.1 s with five queued executions, leaving nothing orphaned). No
  optimisation was performed: nothing measured was outside budget.
- **Failure injection** — database stopped under a live backend (ready 503,
  live 200, `/metrics` 200, recovery in 1 s with no restart), unreachable
  database at boot, SIGKILL, SIGTERM with work in flight, partial workflow
  failure, network interruption, and a genuinely full filesystem (clean HTTP
  500, no crash). Six of seven unsafe configurations refused to boot.
- **Security** — forged/expired/tampered JWTs and `alg=none` all rejected,
  host-header injection blocked, SQL injection parameterised, 413 on oversized
  bodies, rate limiting with `Retry-After`, SSRF guard blocking loopback and
  private ranges.

#### Still not validated

Docker runtime remains **unexecuted** — no container runtime and every registry
unreachable. The long run was 48 minutes, not 24 hours. Multi-replica operation
is untested. See `M9_VALIDATION_REPORT.md` §11.

## [1.1.0-rc1] - 2026-07-27 — Release Candidate 1

### Milestone 7 — Production deployment and release candidate

No new features. M7 attempted, from a clean clone, to do exactly what the
documentation said, and fixed what did not work. Two release-blocking
configuration defects were found that way — neither visible from reading the
code. Full evidence in `M7_RELEASE_AUDIT.md`.

#### Fixed — release blockers

- **`.env` at the repository root was silently ignored (M7-F1, CRITICAL).**
  `Settings.model_config` used `env_file=".env"`, which pydantic-settings
  resolves relative to the **current working directory**. Every guide instructs
  the operator to write `.env` at the repository root and start the server from
  `backend/` — different directories, so the file was never read. The process
  did not fail; it fell back to every default. Reproduced against a real server
  with a fully populated production `.env`: `ENVIRONMENT` became `development`,
  `AUTH_ENABLED` became **false** (every caller treated as a local admin),
  `ENABLE_DOCS` became **true** (Swagger served publicly), and `DATABASE_URL`
  fell back to SQLite while PostgreSQL sat migrated and unused — `/docs`
  returned `200` and `/api/workflows/` returned `500` with
  `no such table: workflows`. The M5 startup gate could not catch it: it only
  refuses to boot when it believes it is in production, and `ENVIRONMENT` had
  itself defaulted back. Fixed with deterministic discovery — the repository
  root and `backend/` are resolved from the module's own path, so the same
  `.env` is found regardless of the working directory. The CWD file is still
  read and still takes precedence, so no existing deployment changes behaviour;
  `CREATOR_OS_ENV_FILE` overrides the search entirely.
- **Custom settings sources discarded their resolved configuration (M7-F2,
  HIGH — present since M6).** The M6 list-friendly sources were constructed as
  `_ListFriendly*Source(settings_cls)` with no further arguments, so every other
  argument fell back to a constructor default and the configuration
  pydantic-settings had already resolved — including the per-instance
  `_env_file` override — was thrown away. `Settings(_env_file=...)` therefore
  ignored the file and returned defaults. Invisible in normal operation (the
  module-level singleton passes no overrides), which is why it shipped in M6;
  found in M7 when the M7-F1 regression tests failed against a *correct* fix.
  The replacements now inherit the resolved attributes from the sources they
  replace, and the `.env` candidate list is re-evaluated at construction time
  rather than frozen at class creation — preserving the pre-M7 timing so
  directory-changing callers and tests behave exactly as before.

#### Added

- **`examples/`** — four production-shaped workflows: a dependency-free smoke
  test, a chained AI content pipeline using the provider fallback chain, a
  resilient HTTP sync demonstrating retries and explicit failure branching, and
  a scheduled batch report using loop fan-out.
- **`scripts/verify_examples.py`** — imports, validates, executes, reads back
  and exports every example against a live backend, asserting the round trip.
  It paid for itself on first run by catching two examples whose
  `{{ Start.topic }}` templates rendered **empty** (the correct reference for a
  seeded variable is `{{ Start.variables.topic }}`).
- **46 tests.** `tests/m7/test_env_discovery_m7.py` and
  `test_settings_sources_m7.py` (23) pin configuration resolution and
  precedence; 6 of them fail against the pre-fix code, confirming they are real
  guards. `tests/m7/test_docker_assets_m7.py` (23) statically validates the
  Docker assets — compose topology, the `${VAR}` contract against
  `.env.production.example`, nginx upstream host and port against real compose
  services, container probe paths against the live FastAPI route table,
  one-shot migration wiring, unprivileged user, `.dockerignore` coverage.
- **`docs/TROUBLESHOOTING.md`, `docs/FAQ.md`, `docs/UPGRADE_GUIDE.md`,
  `docs/M7_RELEASE_AUDIT.md`** — all new.

#### Changed

- `README.md` rewritten (it was a single line).
- `docs/INSTALLATION_GUIDE.md` rewritten — it was 19 lines that named no
  prerequisites and pointed at a build script.
- `docs/RELEASE_NOTES.md` rewritten — it still described 0.3.0-alpha.
- Version set to `1.1.0-rc1` across `app/version.py`, `frontend/package.json`
  (was a stale `1.0.1-alpha`) and the docs.
- `docs/PROJECT_STATUS.md`, `docs/KNOWN_ISSUES.md`, `docs/TEST_COVERAGE.md`,
  `docs/TODO.md`, `docs/ROADMAP_PROGRESS.md` and `release_notes.txt` brought in
  line with measured reality.

#### Verified

Fresh clone → venv → dependencies → migrations → boot → execute a workflow →
graceful shutdown. Production boot on **PostgreSQL 16.2** with the full security
posture asserted (`/docs` 404, unauthenticated API 401, host-header injection
400, bootstrap admin created, JWT login, RBAC, zero config errors, secrets
absent from logs). Migrations upgrade/downgrade/round-trip and a full
downgrade-to-base cycle leaving **zero orphaned enum types**. Backup →
destructive delete → restore recovering every row. Restart persistence.
**1484 passed / 8 skipped** on SQLite (1492 collected); **1492 with zero skips** on PostgreSQL;
**179 frontend**; 89% coverage; clean typecheck and production build; **4/4
examples executed**.

**Closed M6-5:** the 8 PostgreSQL migration regression tests, which had never
run in M5 or M6 for want of a PostgreSQL server, now execute and pass.

#### Still unverified

**Docker.** `docker build` and `docker compose up` have never been executed —
no container runtime in M5, M6 or M7 (no `docker`/`podman`/`nerdctl` binary, no
socket, every registry unreachable, `podman` absent from package sources). Every
process the containers would run has been verified outside them, and the assets
are statically validated, but that is not the same as running the stack.

## [1.1.0] - 2026-07-26

### Milestone 6 — Production validation, scalability and operational readiness

No new features. M6 executed what M5 had only written: the deployment path was
run for the first time against real PostgreSQL 16.2 and a real
production-configured server. That exercise found six defects, three of which
made a documented production deployment impossible. Full evidence in
`M6_VALIDATION_REPORT.md`.

#### Fixed — production blockers
- **Comma-separated list settings crashed the process at import (M6-F1,
  CRITICAL).** `pydantic-settings` JSON-decodes complex fields inside the
  settings source, before field validators run, so the `_split_csv` validator
  was dead code for environment input and `CORS_ORIGINS=a,b` raised
  `SettingsError` during `Settings()`. Because that happens at import, the
  process died before logging or startup validation existed to explain why —
  and the M5 startup-validation gate was therefore unreachable on any
  deployment configured the documented way. Custom env/dotenv sources now
  accept CSV *and* JSON; malformed JSON still errors; source precedence is
  unchanged.
- **The `/api/v1` alias bypassed four path-prefix controls (M6-F2, HIGH).**
  Routers are mounted at both `/api` and `/api/v1`, but the auth rate-limit
  budget, CSRF exemption, credential `Cache-Control` and upload body-size
  exemption all matched the literal `/api` prefix. Measured: 14 consecutive
  logins on `/api/v1` were never throttled while `/api` throttled at 10. Added
  `canonical_path()` and applied it at all four sites. RBAC was *not* affected
  (route dependencies apply to both mounts) — now asserted by test.
- **PostgreSQL downgrade orphaned ENUM types (M6-F3, HIGH).** `DROP TABLE` does
  not remove the native type backing `sa.Enum`, so the rollback procedure in
  `DEPLOYMENT.md` wedged the database: `downgrade` then `upgrade` failed with
  `DuplicateObject: type "executionstatus" already exists`. Both affected
  downgrades now drop their type. SQLite was unaffected, which is why the
  SQLite-only M5 migration tests passed.
- **`.env.production.example` was missing (M6-F4).** Referenced by
  `docker-compose.yml` and `DEPLOYMENT.md`, but `.gitignore`'s blanket `.env.*`
  rule had silently swallowed it, so the documented quick start failed at step
  one. Template added; ignore rule fixed.

#### Fixed — scalability
- **Database connection pool was undersized (M6-F6, HIGH).** Found by load
  testing, not by any existing test. Every in-flight request holds a connection
  for its whole lifetime, so pool capacity — not CPU — caps concurrency. At 100
  concurrent clients the M5 default (5+10=15) produced 16% errors and 7.6 rps
  with p99 of 60 s. Raised to 20+60=80 (the measured knee: 500/500 ok, 0%
  errors, 81.7 rps, p99 4.6 s — a 10.7x throughput and 13x p99 improvement).
  `DB_POOL_TIMEOUT_SECONDS` is now explicit (10 s, was SQLAlchemy's implicit
  30 s) and actually passed to `create_engine`; previously no `pool_timeout`
  was wired in at all. Pool exhaustion now returns `503` + `Retry-After` with a
  stable `database_unavailable` code instead of an opaque `500` with a leaked
  stack trace, and startup validation warns when capacity is below 80.

#### Verified for the first time
- PostgreSQL migrations: upgrade, downgrade, and three full round-trip cycles.
- Production boot with authentication, JSON logging, HSTS, docs disabled.
- Health, readiness (correctly 503 with a named failing dependency), metrics.
- Graceful shutdown (208 ms, orderly teardown), restart idempotency, SIGKILL
  recovery with no data loss and no duplicate bootstrap admin.
- Database outage and **automatic recovery in ~1 s with no app restart**.
- Backup and disaster recovery: `pg_dump` -> `DROP DATABASE` -> `pg_restore`,
  with users, projects, audit history and schema version all intact.

#### Measured limitations (documented, not fixed)
- **Rate limiting is per-process.** Measured: `--workers 4` with a 5/min budget
  admitted 15 of 30 attempts — 3x the configured limit. The execution queue,
  SSE broker and error aggregator are likewise per-process. Redis would fix
  these but was **declined** for M6: it adds a mandatory external service to a
  local-first product and would require rewriting the execution engine, which
  the milestone brief prohibits. Supported scaling today is up, not out
  (`WEB_CONCURRENCY=1`).
- **Docker remains unexecuted.** No container runtime available.

#### Testing
- Backend 1342 -> **1446** (+104). With PostgreSQL: 1446 passed, 0 skipped.
  Without: 1438 passed, 8 skipped (the PostgreSQL migration guards).
- Every new suite was verified to **fail against the pre-fix code** (39 of 104
  fail without the fixes), so they are genuine regression locks.
- Frontend unchanged at 179 passing; typecheck and production build clean.
- Added `scripts/loadtest.py`, the harness that produced the measurements.

### Milestone 5 — Production readiness and platform hardening

Turns a functional application into a deployable platform. No new product
features: this milestone is security, reliability, observability, deployment
and verification. Full pre-work audit in `M5_GAP_ANALYSIS.md`.

#### Security — authentication and authorization
- **Added authentication.** The platform previously had no notion of *who* was
  calling: all ~80 endpoints were anonymous. Added `users`, `api_keys` and
  `refresh_sessions`, PBKDF2-HMAC-SHA256 password hashing (600k iterations,
  per-password salt), and a `Principal` abstraction every router reasons about.
- **RBAC is now enforced.** `require_permission` existed since M0 but *no route
  ever called it*, so the role model was decorative. Added FastAPI dependencies
  (`require_read`/`require_write`/`require_manage_users`/…) applied per
  endpoint, reusing the existing `ROLE_PERMISSIONS` map rather than inventing a
  second model.
- **JWT (HS256), dependency-free.** The `alg` header is pinned before
  verification, so `alg: none` and algorithm-confusion attacks are rejected;
  signatures are compared in constant time; `exp`/`nbf`/`iss`/`aud`/`typ` are
  all validated, so a refresh token cannot be replayed as an access token.
- **Refresh rotation with theft detection.** Reusing a consumed refresh token
  revokes every session for that user.
- **API keys** with SHA-256 storage (plaintext shown once), optional expiry and
  scopes. Scopes *intersect* the owner's role, so a key can only narrow
  authority, never escalate.
- Login lockout, and uniform failure timing/messaging so the endpoint cannot be
  used to enumerate accounts.
- An administrator can no longer deactivate or demote themselves, which would
  leave an instance with nobody able to manage users.
- Added CSRF (double-submit) for cookie flows, `TrustedHost` validation, HSTS,
  `Cache-Control: no-store` on credential responses.
- **Rate limiting is now credential-keyed** and honours `X-Forwarded-For` only
  when `TRUST_PROXY_HEADERS` is set — previously every caller behind a proxy
  shared one bucket, and the header could be spoofed. Login gets a separate,
  stricter budget.

#### Script sandbox
- **Python nodes now execute in a separate OS process** with kernel-enforced
  `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`, `RLIMIT_NPROC` and `RLIMIT_CORE`.
  This fixes two defects the M4 in-process `exec` could not: `while True: pass`
  pinned a CPU core for the life of the backend (a thread cannot be cancelled),
  and a large allocation OOM-killed the whole service.
- Added a **PEP 578 audit hook**, which is the actual enforcement boundary. The
  documented `__subclasses__` → `BuiltinImporter.load_module` escape still
  yields a module reference, but every dangerous operation on it (file open,
  `system`, `fork`, `kill`, `chmod`, `remove`, sockets, `listdir`) raises an
  audit event and is refused. Verified post-escape in tests.
- Scrubbed child environment (no API keys, no `DATABASE_URL`), private temp
  working directory, import allowlist, network denial, per-execution quota.
- **Honestly documented as defence in depth, not a security boundary.** The
  JavaScript node is *not* sandboxed. Script nodes remain disabled by default.
  See `SECURITY.md` §5.

#### Reliability
- **Startup validation** refuses to boot an unsafe production configuration
  (auth disabled, wildcard CORS, placeholder secret, shell executor without an
  allowlist, SSRF enabled). Warnings only outside production, so local
  development is unaffected.
- `/health/ready` now checks the worker pool, queue depth and configuration
  findings, and returns **503 when degraded** so an orchestrator can act on it.

#### Observability
- Dependency-free Prometheus registry and `/metrics` exposition. Path labels
  use the route template and unmatched paths collapse, so metric cardinality
  stays bounded under scanning.
- Added `correlation_id` alongside `request_id`, propagated via contextvars.
- Bounded in-process error aggregation at `/api/system/errors`.

#### Database
- Migration `d5f3a7c81b64` adds the identity tables and **finally creates
  `audit_events`**, which had existed as an ORM model since V1.0 with no
  migration at all — a migration-only deployment started without the table and
  audit writes failed at runtime (M4 known issue #10).
- Added a migration test asserting **every ORM table has a migration**, so this
  class of drift cannot recur. Upgrade → downgrade → re-upgrade is exercised.

#### API
- Added `/api/v1` versioned routes. The unprefixed `/api` paths remain
  permanently supported.

#### Frontend
- **The workflow editor is now actually reachable.** `App.tsx` rendered static
  placeholder text for the Workflows tab, so the entire M3/M4 editor was
  unreachable from the running application.
- Mounting it exposed a latent defect: **20 of the 22 node component files were
  committed as zero-byte files** in M3/M4. Nothing caught this because the
  modules were never bundled. All 20 are now implemented, with config fields
  mirroring the backend node schemas.
- Added an `ErrorBoundary` per tab panel, so one failing section cannot blank
  the application.
- ARIA tabs pattern with arrow/Home/End navigation and roving tabindex.
- The health check is abortable and retryable and no longer sets state after
  unmount.
- Converted type-only imports throughout; `vite build` is now warning-free.

#### Deployment
- Added backend and frontend Dockerfiles (multi-stage, non-root, healthchecks),
  `docker-compose.yml` with PostgreSQL and a separate one-shot migration
  service, nginx config with SSE-safe proxying, `.env.production.example`, and
  `.dockerignore` for both contexts.
- Added `psycopg[binary]`: compose specified `postgresql+psycopg://` but the
  driver was not a dependency, so the documented production database could not
  actually connect.

#### Documentation
- Added `SECURITY.md`, `DEPLOYMENT.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`
  and `M5_GAP_ANALYSIS.md`.

#### Testing
- **257 new backend tests** (1085 → 1342) and **74 new frontend tests**
  (105 → 179), covering authentication, authorization, JWT, API keys, CSRF,
  rate limiting, security headers, the sandbox, startup validation, health
  probes, metrics, error aggregation and migrations.
- Sandbox tests include a `TestDocumentedLimitations` class asserting the
  weaknesses that are *not* fixed, so the documentation cannot silently drift.
- Fixed the CI workflow's frontend step, which invoked a script that does not
  exist (`npm run test:run --if-present`) and therefore ran no tests at all.

#### Defects found by the M5 self-audit (and fixed)

Two issues were caught *after* the feature work was written and passing its
own tests. Both are recorded here because they show what the tests missed.

- **Authorization was only wired into 2 of 9 routers.** The dependency worked
  and was unit-tested; it simply had not been *applied*. With `AUTH_ENABLED`
  on, a `viewer` — or an anonymous caller — could still create and delete
  workflows and projects, register plugins, read the audit log and write
  forged audit entries. Fixed with router-level defaults that fail closed
  (`require_method_permission`), plus `TestRouteCoverage`, which walks the live
  route table and fails if any non-public route lacks an authorization
  dependency.
- **Refresh-token rotation had a TOCTOU race.** It read `revoked_at`, decided,
  then wrote, so eight concurrent rotations of one token produced three valid
  sessions — which also defeats the replay detection meant to catch exactly
  that. Replaced with an atomic conditional `UPDATE`. The regression test uses
  a file-backed database on purpose: the suite's in-memory `StaticPool` shares
  one connection across threads and hides the race entirely.
- **`POST /api/enterprise/audit` accepted a client-supplied `user_id`**, making
  the audit trail forgeable. It now records the authenticated principal.

Audited with no defects found: metrics registry under 8-thread contention,
error-aggregator bounding, and sandbox file-descriptor/process leaks across 12
concurrent runs and 4 timeout kills (zero fd delta, zero zombies).

#### Known limitations (unchanged or newly documented)
- Single-process execution only; the queue is in-memory and is lost on restart.
  Running more than one replica risks double execution.
- Rate limiting and SSE fan-out are per-process.
- RBAC is global; there is no per-workflow ownership or tenancy.
- The JavaScript node is not sandboxed.
- Audit coverage is partial (auth and user administration only) and not
  tamper-evident.
- The deployment assets have not been executed end to end — no container
  runtime was available during M5 development.
- CI has still never run: activating it requires a maintainer to move the
  workflow into `.github/workflows/` (GitHub App `workflows` permission).

### Milestone 4 — Execution engine and AI orchestration

Turns the visual editor into an executable platform: a workflow built on the
canvas now runs end to end with live feedback.

#### Workflow execution engine
- Added `run_execution_v2`: branch gating, bounded loops, pause/resume/stop,
  run variables, live streaming and per-node metrics. The M1 `run_execution`
  is preserved verbatim, so the pre-M4 code path and its tests are unchanged.
- **Conditional branching now works.** The engine previously ignored branch
  results and `Edge.label` entirely, executing *both* sides of every condition.
  It now follows only matching labels and cascades suppression to descendants
  that lose all inbound paths.
- **Loops.** A cycle is legal when its closing edge is labelled `loop`; those
  edges are excluded from dependency ordering so the schedulable graph stays a
  DAG. Any other cycle is still rejected. Iteration is capped three ways.
- **Pause / resume / graceful stop.** `ExecutionStatus.PAUSED` existed since
  V1.0 but nothing ever set or honoured it. Added `ControlHandle` with
  thread-safe requests and loop-affine waiting.
- **Bounded priority queue and worker pool** replace unbounded
  `asyncio.create_task`. Over capacity returns HTTP 429 instead of degrading
  the process. Priorities: critical/high/normal/low, FIFO within a band.
- Engine-level aborts (wall-clock timeout, node-execution cap) now fail the run.
  An earlier build reported COMPLETED while recording an error.

#### Node execution
- Added a unified node runtime: declarative input/output schemas, coercion
  (every editor input arrives as a string), validation, metrics and a stable
  error taxonomy. Validation/permission errors are **non-retryable**, so a bad
  config fails in one attempt instead of three.
- **Added all 23 editor node types.** The M3 palette exposed 22 types while the
  backend implemented 10 *different* ones — the intersection was `{delay}`, so
  saving any editor-built workflow returned HTTP 422. 83 names including
  snake_case aliases; pre-existing M1 types keep their original executors.
- Node outputs are truncated at `EXECUTION_MAX_OUTPUT_BYTES`; non-serialisable
  outputs are replaced with an explicit marker rather than failing at the DB.

#### Real-time execution
- Added `ExecutionBroker` with per-execution fan-out, **bounded drop-oldest**
  subscriber queues (a slow client cannot stall the engine), a replay buffer for
  reconnects and batched log writes.
- Added SSE `GET /api/executions/{id}/stream` plus a polling fallback.
- Added durable `workflow_execution_logs` with sequencing, level and node
  attribution.

#### AI orchestration
- Added `orchestrator.generate()` with a provider fallback chain.
- **Wired up the circuit breaker** — `AI_CIRCUIT_BREAKER_*` had existed since M1
  but was never used. CLOSED → OPEN → HALF_OPEN probe.
- Added a cost model, token accounting, execution tracing, prompt templating and
  registration hooks for image/TTS/STT providers.
- Fixed `_provider_available` hardcoding provider names, which silently dropped
  runtime-registered providers from the fallback chain.

#### Execution history
- Added global search/filter (workflow, status, trigger, date, text), replay,
  resume-failed, timelines, lineage and aggregate stats.
- Resume-failed seeds completed node outputs into the new run. **It re-traverses
  the graph**, so it is a retry with context, not mid-graph resumption.

#### Frontend
- Added run/pause/resume/stop/cancel/replay controls, live node status, a
  progress bar, a streaming log viewer (level filter, search, auto-scroll) and
  an execution history panel with replay actions.
- Replaced the M3 `ExecutionPanel` placeholder, which ran an empty
  `setInterval` and rendered state nothing ever populated.
- **Fixed the graph save contract (the editor could never save).** The editor
  sent `{id: uuid-string, type, position, data}`; the backend expected
  `{id: int, node_type, position_x, source_id}`. Every save returned 422. Added
  `graphAdapter.ts` as the single conversion point, which also carries React
  Flow's `sourceHandle` through as the edge branch label.
- **Fixed undo/redo, which never worked.** History started empty at index -1, so
  the first edit could never be undone.

#### Testing
- Backend: **825 → 1085** tests (260 new), all passing.
- Frontend: **0 → 105** runnable tests. M3 committed five vitest files but the
  project had no runner, no jsdom and no `test` script, so none could execute;
  vitest + jsdom + testing-library are now configured and those files run.

#### Performance
- Node persistence reduced from 4 transactions per node to 1 per outcome.
- Graph load reduced from 4 sessions to 1.
- Batched log writes; composite indices on `(workflow_id, status)` and
  `(status, priority, id)`.
- Admission control replaces unbounded task creation.

#### Fixed (found by the new tests)
- SSE endpoint hung forever when an execution had already finished: the
  backfilled terminal event was replayed but never inspected.
- `PUT /graph` rejected valid loop workflows (loop-unaware validator).
- `/node-schemas` hid canonical types behind alphabetically-earlier aliases.
- `classify_exception` mislabelled `RuntimeError`/`IndexError`/`AttributeError`.
- Worker pool started lazily inside request scope, leaking long-lived tasks.
- Circular import between `runtime` and `executors` silently disabled the whole
  M4 node library depending on import order.

#### Known limitations
- Single-process only; the in-memory queue is lost on restart.
- `python`/`javascript` nodes are restricted interpreters, **not** sandboxes,
  and are disabled by default.
- Resume-failed re-traverses the graph.
- The engine's global write lock still serialises status writes across runs.
- No inbound webhook triggers; no image/TTS/STT providers ship by default.


### Milestone 2 — Service completion: AI runtime and media pipeline

#### AI Runtime
- Added conversation CRUD, message append/list endpoints, complete model registry CRUD, provider introspection, chat completion response metadata, token usage rows and aggregate token reporting.
- Added validation for model types, providers, message roles and empty chat input.
- Added context-window trimming by message count and estimated token budget while preserving the leading system prompt and most recent turns.
- Preserved V1.0 orchestrator string-compatibility while returning richer API metadata.

#### Media System
- Added secure media asset CRUD with upload, registration, download, update, delete and list endpoints.
- Restricted all storage operations to `MEDIA_ROOT`, rejecting path traversal, absolute path escapes, Windows drive escapes, null bytes and symlink escapes.
- Added streaming upload size enforcement, SHA-256 hashing and MIME/media-type detection from file content.

#### Media Processing / FFmpeg
- Moved media processing to a bounded background worker pool with job status/progress reporting.
- `POST /api/media/{asset_id}/process` now returns `202 Accepted`; `wait=true` remains available for compatibility.
- Added `ffprobe` metadata extraction, `ffmpeg` video poster generation, Pillow image poster generation and structured graceful fallbacks when FFmpeg is absent or a file cannot be probed.
- Added Alembic migration `b7d9f8a2c1e3` for media asset content metadata and processing-job progress/results.

#### Tests
- Backend suite grown from **425 to 825 tests**; coverage **91% → 94%**.

### Milestone 1 — Backend core hardening

#### Security
- **Fixed remote code execution**: the `shell_command` node executor ran
  arbitrary strings through a shell via an unauthenticated API. It is now
  **disabled by default** (`ALLOW_SHELL_EXECUTOR=false`), requires an explicit
  command allowlist, never spawns a shell (so `;`, `|`, `$()` cannot chain
  commands), and enforces a timeout that kills the process.
- **Fixed SSRF**: the `http_request` executor accepted any URL. It now rejects
  non-HTTP(S) schemes, blocks private/loopback/link-local/reserved addresses and
  cloud metadata endpoints (including via DNS resolution), enforces timeouts,
  caps redirects, and truncates oversized responses.
- Added security response headers (CSP, `X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, COOP).
- Added a request body-size limit (HTTP 413) and an in-process rate limiter.
- Internal exceptions no longer leak stack traces or messages to API clients.
- Credentials (API keys, bearer tokens, passwords) are redacted from all logs.
- `math_expression` node evaluates only a character-restricted arithmetic
  grammar; code injection attempts are rejected.

#### Fixed
- **Workflow engine busy-wait**: when tasks were pending but none were runnable
  the scheduler spun the event loop at 100% CPU. Scheduling is now event-driven
  and unreachable nodes are marked `SKIPPED`.
- **Data corruption under parallel execution**: concurrent node-status writes
  raced on SQLite inserts, producing `FlushError: NULL identity key` and losing
  node results. Engine persistence writes are now serialised.
- **Falsy node outputs were discarded**: `if result:` dropped `{}`, `0` and
  `False` outputs. Now only `None` is treated as "no output".
- Cycles are detected up front with a precise message instead of surfacing as a
  misleading "deadlock" error.
- `get_db()` now rolls back on exception instead of leaking a dirty session.
- Event bus subscribers no longer abort the publish when one raises.
- Plugin SDK hook failures are logged instead of silently swallowed.

#### Added
- Typed error hierarchy (`app/core/errors.py`) with a single stable JSON error
  envelope and request-id correlation on every response.
- Structured logging: optional JSON output, rotating file handler, secret
  redaction, and per-request correlation ids.
- Per-node execution policy: configurable retries, backoff, timeout and
  `on_error` behaviour (`fail`, `continue`, `skip_branch`).
- Bounded workflow concurrency via `WORKFLOW_MAX_PARALLEL_NODES`.
- Execution checkpointing into `WorkflowExecution.state`, plus progress events
  on the event bus for live UI updates.
- Six new node types: `noop`, `delay`, `math_expression`, `template`,
  `transform`, `branch` — with `{{ reference }}` templating between nodes.
- Node/edge/execution CRUD, full-graph save, and graph validation endpoints
  (the persistence contract for the V1.1 visual editor).
- Plugin, enterprise/audit and system/introspection routers.
- `/health/live` and `/health/ready` probes; `/api/system/metrics`.
- Async event-bus subscribers, wildcard subscriptions and a bounded event
  history feed.
- Plugins can contribute workflow node types through the SDK.
- Alembic migration `a1b2c3d4e5f6` adding editor geometry, retry policy,
  execution timing columns and indices on all hot foreign keys.

#### Performance
- SQLite tuned with WAL journaling, `synchronous=NORMAL`, a 16 MB page cache,
  in-memory temp store and a busy timeout.
- Indices added to every hot foreign key (`workflow_id`, `execution_id`,
  `node_id`, `status`, `created_at`).
- Gzip compression for responses over 1 KB.
- Routers imported lazily; ORM sessions held only for short transactions and
  detached node snapshots passed to executors.

#### Tests
- Backend suite grown from **20 to 425 tests**; coverage **82% → 91%**.

### Milestone 0 — Build repair and hygiene
- Fixed `electron/main.ts` syntax error (TS1005) that broke `npm run build` on
  `main`.
- Fixed the failing `test_settings_defaults` by centralising the version in
  `app/version.py`.
- Untracked build artifacts (`__pycache__`, `.coverage`, `creator_os.db`,
  `dist-electron/main.js`) and hardened `.gitignore`.
- Added CI pipeline definition and `scripts/ci-local.sh`.
- Published `docs/V1_1_GAP_ANALYSIS.md`.

## [1.0.1-alpha] - 2026-07-26
### Changed
- Migrated FastAPI `on_event` startup/shutdown hooks to modern `asynccontextmanager` lifespans to prevent warnings and technical debt.
- Test coverage infrastructure instantiated leveraging `pytest-cov`.
- Added missing integration tests for REST API routers.

## [1.0.0] - 2026-07-26
### Added
- Workflow Engine (DAG execution)
- AI Runtime
- Media Pipeline
- Plugin Architecture
- Desktop UI
- Enterprise capabilities (Audit Logging, RBAC)
