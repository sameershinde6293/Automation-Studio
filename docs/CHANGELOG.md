# Changelog

## [1.1.0] - 2026-07-26 (in progress)

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
