# Changelog

## [1.1.0] - 2026-07-26 (in progress)

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
- **228 new backend tests** (1085 → 1313) and **74 new frontend tests**
  (105 → 179), covering authentication, authorization, JWT, API keys, CSRF,
  rate limiting, security headers, the sandbox, startup validation, health
  probes, metrics, error aggregation and migrations.
- Sandbox tests include a `TestDocumentedLimitations` class asserting the
  weaknesses that are *not* fixed, so the documentation cannot silently drift.
- Fixed the CI workflow's frontend step, which invoked a script that does not
  exist (`npm run test:run --if-present`) and therefore ran no tests at all.

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
