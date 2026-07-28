# Known Issues

## M9 (release candidate 3) — current

M9 ran Creator OS on a production-shaped staging deployment backed by **real
PostgreSQL 16.2**, measured it, and injected failures. Four defects were found
and fixed (`M9_VALIDATION_REPORT.md`). What remains open is below, followed by
everything carried forward from earlier milestones.

| # | Issue | Impact | Workaround |
| --- | --- | --- | --- |
| M9-1 | **Docker runtime still never executed** — fourth consecutive milestone | Image build, `docker compose up`, container networking, volume persistence across recreate, in-container HEALTHCHECK execution, restart policies and enforcement of the `cpus`/`memory` limits are all unverified. No container runtime exists here (no `docker`/`podman`/`nerdctl`, no `/var/run/docker.sock`) and every registry is unreachable | 44 static checks and 53 asset tests pass, and every *process* the containers would run has now been executed outside them against the same PostgreSQL 16.2 — including backup and restore. **Treat the first containerised deployment as a validation exercise**; use `scripts/deploy.sh` |
| M9-2 | **Long run was 48 minutes, not 24 hours** | Slow leaks of a few MB/hour, log rotation at the 10 MB boundary, `pool_recycle` at 1800 s and multi-hour scheduler drift are unproven. Within the 48-minute window there were 0 failures, RSS grew 91→100 MB tracking database growth, and FDs/threads stayed bounded | The sandbox does not persist across sessions (a reset destroyed an earlier run mid-flight). Run a 24-hour soak on real infrastructure before declaring GA |
| M9-3 | **Single-node only** | No multi-replica run, no PgBouncer, no load balancer. `WEB_CONCURRENCY>1` multiplies the in-process rate limit and gives each worker its own execution queue; neither effect has been measured | Keep `WEB_CONCURRENCY=1` until measured, and read the Scalability section of `DEPLOYMENT.md` before scaling out |
| M9-4 | SSRF guard blocks the reserved documentation ranges | `192.0.2.0/24`, `198.51.100.0/24` and `203.0.113.0/24` are classified `is_private` by Python's `ipaddress`, so workflow HTTP nodes cannot reach them. Conservative in the right direction, but it makes those ranges unusable as test targets | None needed in production. Use a real public host when testing HTTP nodes |
| M9-5 | No TLS termination executed | The nginx and Caddy configs are validated statically but neither has served a request here (no nginx binary available) | Run `nginx -t` on the deployment host before cutting over |

### Resolved in M9

| Issue | Severity | Resolution |
| --- | --- | --- |
| **`backup.sh` reported success while producing no database backup.** Exit `0` with no dump: missing `pg_dump` was a warning, a failing `pg_dump` was swallowed by `|| echo`, the connection was rebuilt from `POSTGRES_*` (dropping host and port), and the media archive ignored `MEDIA_ROOT`. `restore.sh` mirrored it — `psql "${DATABASE_URL:-}"` with no `ON_ERROR_STOP` | **Critical** | Both scripts fail loudly, use `DATABASE_URL`, fall back to a bundled `pg_dump`/`psql`, verify with `gunzip -t` + size check, checksum artefacts, honour `MEDIA_ROOT`, and use `ON_ERROR_STOP=1`. Proven by a dump → drop → restore → boot → authenticate drill (M9-F3) |
| **Database pool saturation invisible in `/metrics`** — the documented concurrency limit had no telemetry, so exhaustion looked identical to a slow database | High | Six `creator_os_db_pool_*` gauges refreshed on scrape; verified moving under 40 concurrent clients (M9-F1) |
| **Account lockout never written to the audit trail** — visible only as a log line | Medium | `auth.account.locked` emitted with username, failure count and `locked_until`; verified in PostgreSQL (M9-F2) |
| **Shipped version disagreed with published version** — docs said rc2, code said rc1 | Low | Unified on `1.1.0-rc3` and enforced by `test_release_consistency_m9.py` (M9-F4/F5) |

---

## M7 (release candidate) — carried forward

M7 executed the documented installation and deployment procedure from a clean
clone. Two release-blocking configuration defects were found and fixed
(`M7_RELEASE_AUDIT.md`). What remains open is below, followed by everything
carried forward.

| # | Issue | Impact | Workaround |
| --- | --- | --- | --- |
| M7-1 | **Docker still never executed** — third consecutive milestone | Image build and `docker compose up` remain unverified. No container runtime exists in this environment: no `docker`/`podman`/`nerdctl` binary, no `/var/run/docker.sock`, every container registry unreachable, and `podman` absent from the configured apt sources | Every *process* the containers would run has been verified outside them (same PostgreSQL 16.2, production settings, Uvicorn command line, probes, migration and backup commands), and 23 static tests in `tests/m7/test_docker_assets_m7.py` validate compose topology, the `${VAR}` contract, nginx upstream/port, probe paths against the live route table, and image hardening. **Treat the first containerised deployment as a validation exercise** |
| M7-2 | No `LICENSE` file | All rights reserved by default, which blocks reuse, forking and distribution | A maintainer must choose and add one |
| M7-3 | Log rotation rollover not exercised | `RotatingFileHandler` is configured at 10 MB × 5 backups and writes correctly, but a rollover was never triggered — that needs 10 MB of output | Low risk; standard-library behaviour |
| M7-4 | Electron desktop shell not launched | The binary download is blocked here, so `npm run electron:dev` / `electron:build` are unexercised. The browser app, tests, typecheck and production build all pass | Run `npm install` without `ELECTRON_SKIP_BINARY_DOWNLOAD=1` on an unrestricted network |
| M7-5 | Outbound HTTPS fails behind a TLS-inspecting proxy | `httpx` validates against the `certifi` bundle, which lacks a corporate proxy CA, so HTTP/webhook nodes fail with `CERTIFICATE_VERIFY_FAILED` while `curl` succeeds. **Environmental, not a product defect** — proven: the identical request returns `200` against the system trust store | Start with `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` |
| M7-7 | **`test_path_label_uses_the_route_template` can flake** (pre-existing M5 test, untouched by M7) | Observed **once in five full PostgreSQL runs** during M7 final validation. The test asserts `"12345" not in body` after requesting `/api/workflows/12345`, to prove metric path labels use the route template rather than raw ids. It failed because `12345` appeared inside an unrelated **float timing value** (`0.09123452199992244`) in the histogram output — a substring collision, not a labelling bug. Verified: **no `path=` label ever contained a raw id**, and the test passed 5/5 in isolation and on a full re-run (1492 passed). **Not a product defect**; the metric cardinality protection works | Re-run. A durable fix would scope the assertion to `path=` labels rather than the whole response body — deliberately not changed here, since editing a working test to chase a rare collision is out of M7 scope |
| M7-6 | `backend/main.py` V1.0 stub still present | Starting `uvicorn main:app` serves an app with no routers. **Verified still in use** by `tests/test_main.py`, so it is not dead code and was not removed under the "remove only verified dead code" rule | Always start `app.main:app`. Flagged since M5 (A5) |

### Resolved in M7

| Issue | Severity | Resolution |
| --- | --- | --- |
| **`.env` at the repository root silently ignored** — the process fell back to every default, booting in `development` with authentication **off**, Swagger **exposed** and SQLite instead of the migrated PostgreSQL. Startup validation could not catch it because `ENVIRONMENT` had itself defaulted back | **Critical** | Deterministic `.env` discovery resolved from the module's own path; CWD still wins, so no existing deployment changes behaviour (M7-F1) |
| **Custom settings sources discarded their resolved configuration**, so `Settings(_env_file=...)` silently returned defaults | High | Sources now inherit the resolved attributes; candidate list re-evaluated at construction time (M7-F2, present since M6) |
| **M6-5** — PostgreSQL migration regression tests had never executed | Medium | Run against real PostgreSQL 16.2: **8 passed**, and the full suite passes with **zero skips** |
| Two example workflows rendered `{{ Start.topic }}` as empty | Medium | Corrected to `{{ Start.variables.topic }}`; all four examples now execute, asserted by `scripts/verify_examples.py` |

## M6 (production validation) — carried forward

| # | Issue | Impact | Workaround |
| --- | --- | --- | --- |
| M6-1 | ~~**Docker still never executed**~~ — **superseded by M7-1** | Image build and `docker compose up` remain unverified — no container runtime was available in M5 *or* M6. M6 did reproduce the container's runtime contract outside a container (same Postgres, production settings, Uvicorn command line and probes), which is how the M6-F1 boot failure was found | Treat the first real deployment as a validation exercise. Everything the container would run has been verified outside it |
| M6-2 | Concurrency ceiling ≈ DB pool capacity | Each in-flight request holds a connection for its whole lifetime, so ~100 concurrent authenticated requests per instance at the default 80. Excess is shed as `503` + `Retry-After` — correct, but a ceiling | Raise `DB_POOL_SIZE`/`DB_MAX_OVERFLOW`, keeping capacity x replicas under PostgreSQL `max_connections`. Measured curve in `M6_VALIDATION_REPORT.md` §4.3 |
| M6-3 | SSE `cleanup()` can drop a concurrent subscriber's replay buffer (M6-F5) | Two clients disconnecting in the same scheduling window can cost the second its reconnect backfill. **Not** data loss — `flush_logs` runs first, so persisted logs are intact | Reconnect without `after_sequence` to re-read from the database. Deliberately not fixed in M6: the correct fix is broker refcounting, and rewriting working concurrency code for a benign rare race is out of scope |
| M6-4 | **Two timing-sensitive tests can flake under full-suite CPU contention** (*not observed in any M7 run*) — `tests/m2/...::test_process_returns_202_and_progress_can_be_polled` (polls an async media job for ≤0.8 s) and `tests/m4/...::test_pause_then_resume_completes` (3 s `wait_for` budgets around real sleeps) | Observed **1 failure across ~9 full-suite runs**, never the same test twice. Neither reproduces in isolation (0/6 and 0/5 targeted runs). Confirmed **not caused by M6**: the pristine M5 commit `a92af1b` was re-run and the M4 path uses in-memory SQLite, which takes no pool settings, so the M6-F6 change cannot reach it. Running `tests/m6/` before `tests/m4/` also showed no interference | Re-run the file in isolation. A durable fix means replacing wall-clock budgets with deterministic synchronisation — deliberately out of M6 scope, since editing working tests to chase a scheduling artifact risks masking a real regression |
| M6-6 | **Rare `psycopg` C-extension segfault** (*not observed in M7's PostgreSQL runs*) — observed once during a PostgreSQL-enabled full-suite run | One occurrence in five PG-enabled runs, inside `psycopg_binary` during a threaded ORM flush — a driver-level crash, not a Python exception, so no test "failed". Mitigated by tightening connection hygiene in the M6 fixtures (schema reset now disposes its engine in a `finally`); four subsequent full runs were clean (1446/1446, zero segfaults). **Not proven eliminated** — a C-extension crash cannot be ruled out by absence alone | If CI hits it, re-run. A definitive fix would mean isolating PG tests in their own pytest process (`-p forked` or a separate CI job) |
| M6-5 | ~~PostgreSQL migration regression tests skip unless `TEST_POSTGRES_URL` is set~~ — **CLOSED in M7**: executed against real PostgreSQL 16.2, 8 passed, full suite runs with zero skips. Original text: | Unset, 8 tests skip and the M6-F3 enum-cleanup regression is unguarded. **Addressed in `ci/github-actions-ci.yml`**: the new `migrations-postgres` job supplies a PostgreSQL 16 service container and asserts via the JUnit report that the tests actually executed, failing the build if they all skipped. Still gated on CI being activated at all (M5 issue #11) | Set `TEST_POSTGRES_URL` in any other runner; locally the suite skips cleanly by design |

## M5 (production hardening) — carried forward

| # | Issue | Impact | Workaround |
| --- | --- | --- | --- |
| 1 | Single-process execution; the priority queue is in-memory | Queued runs are lost on restart (rows persist as QUEUED but are never re-claimed). Running >1 replica risks **double execution** of the same row | Keep `WEB_CONCURRENCY=1` and one backend replica; re-trigger runs after a restart |
| 2 | Rate limiting is per-process and in-memory | With N processes the effective limit is N× the configured value. **M6 measured this**: with `--workers 4` and a 5/min budget, 15 of 30 credential attempts were admitted — 3x the limit | Single process (`WEB_CONCURRENCY=1`), or terminate rate limiting at a proxy |
| 3 | SSE broker is per-process | A client connected to replica A sees no events from executions on replica B | Single replica |
| 4 | Python sandbox is **not a security boundary** | A CPython escape yields the backend user's OS privileges. `os.stat` raises no audit event in 3.11, so file metadata is readable post-escape | Left disabled by default; run untrusted code in a container with seccomp. See `SECURITY.md` §5 |
| 5 | The JavaScript node is **not sandboxed at all** | Enabling it grants local code execution with the backend user's permissions | Left disabled by default; only enable for trusted authors |
| 6 | RBAC is global, not per-resource | Any `editor` can modify any workflow; no ownership, ACLs or tenancy | Do not use a single instance to isolate mutually untrusting users |
| 7 | Access tokens cannot be revoked before expiry | A stolen access token stays valid for up to `AUTH_ACCESS_TOKEN_TTL_SECONDS` (default 15 min). Only refresh sessions are stateful | Shorten the TTL; deactivating a user kills refresh but not the live access token |
| 8 | No MFA, SSO/OIDC or password reset flow | An administrator must reset forgotten passwords | — |
| 9 | Audit coverage is partial and not tamper-evident | Auth and user administration are audited; workflow/media mutations are not. `POST /api/enterprise/audit` still accepts a caller-supplied `user_id` | Do not treat self-reported audit entries as trustworthy attribution |
| 10 | ~~Deployment assets have never been executed~~ | **Partially resolved in M6, further in M7.** PostgreSQL migrations, production boot, health/readiness/metrics, graceful shutdown, restart, rollback and backup/restore are now all verified with real tooling. Only the Docker layer remains unexecuted — see M6-1 | — |
| 11 | CI has never run | `ci/github-actions-ci.yml` is not in `.github/workflows/`; the automation account lacks the GitHub App `workflows` permission and the push is rejected | A maintainer must copy it in (see `ci/README.md`). `./scripts/ci-local.sh` runs the same checks |
| 12 | No external security review or penetration test | Controls are verified only by this project's own tests | — |
| 13 | Engine `_write_lock` is process-wide | Node status writes serialise across concurrent runs | Acceptable at current scale |
| 14 | `resume_failed` re-traverses the whole graph | Completed non-idempotent nodes re-execute | Use replay when side effects are not idempotent |
| 15 | No inbound webhook triggers | The `webhook` node is outbound only | Trigger runs via the REST API |
| 16 | No image/TTS/STT provider ships by default | Those nodes fail with a clear `provider` error | Register one via `ai_orchestrator.register_*_provider` |
| 17 | AI traces and error aggregation are in-memory and bounded | Lost on restart; no cross-replica view | Use `GET /api/ai/usage` for durable token accounting |
| 18 | No antivirus or archive-bomb protection on uploads | A malicious upload is stored as-is | Scan `MEDIA_ROOT` out of band |
| 19 | Electron postinstall fails behind TLS-inspecting proxies | `npm install` errors on the binary download | `ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm ci` (tests/build/typecheck all work) |

## Resolved in M5

| Issue | Severity | Resolution |
| --- | --- | --- |
| No authentication on any API endpoint | Critical | Users, API keys, refresh sessions, JWT auth (M5) |
| Authorization applied to only 2 of 9 routers — a viewer or anonymous caller could create/delete workflows, register plugins and forge audit entries | Critical | Found by the M5 self-audit. Router-level fail-closed defaults, plus a route-table coverage test (M5) |
| Refresh-token rotation had a TOCTOU race: one token could yield several valid sessions | High | Found by the M5 self-audit. Atomic conditional UPDATE (M5) |
| `POST /api/enterprise/audit` took the actor from a client-supplied `user_id`, so the audit trail was forgeable | High | Records the authenticated principal; requires `manage_settings` (M5) |
| RBAC defined but never enforced anywhere | Critical | `require_permission` dependencies applied per endpoint (M5) |
| Script nodes could pin a CPU core indefinitely | Critical | `RLIMIT_CPU` in a child process; a thread could not be cancelled (M5) |
| Script nodes could OOM-kill the whole backend | Critical | `RLIMIT_AS` (M5) |
| `audit_events` had no Alembic migration (since V1.0) | High | Migration `d5f3a7c81b64`, plus a test asserting every ORM table has one (M5) |
| **20 of 22 node components were empty files** | High | All implemented; `nodeRegistry.test.tsx` renders every type (M5) |
| The workflow editor was unreachable from the running app | High | `App.tsx` now mounts it (M5) |
| No error boundary — one render error blanked the app | High | `ErrorBoundary` per tab panel (M5) |
| Production could boot with unsafe configuration silently | High | Startup validation refuses to start (M5) |
| Rate limiter collapsed all proxied clients into one bucket | High | Credential-first keying; `X-Forwarded-For` only when trusted (M5) |
| CI frontend step ran no tests (`npm run test:run` does not exist) | High | Calls `npm test`; migration and docker jobs added (M5) |
| `postgresql+psycopg://` documented but the driver was missing | High | Added `psycopg[binary]` (M5) |
| No CSRF, trusted-host or HSTS controls | Medium | All added (M5) |
| No container images, compose stack or production env template | Medium | Added (M5) |
| No Prometheus metrics or error aggregation | Medium | Added (M5) |
| Backend coverage figure was stale (claimed 94% since M2) | Low | Re-measured: **89%** (M5) |

---

## Historical

### M4 (execution engine) — superseded by the table above

| # | Issue | Impact | Workaround |
| --- | --- | --- | --- |
| 1 | Single-process execution; the priority queue is in-memory | Queued runs are lost on restart (rows persist as QUEUED but are not re-claimed) | Re-trigger runs after a restart |
| 2 | `python` / `javascript` nodes are restricted interpreters, **not** sandboxes | Enabling them grants local code execution to any workflow author | Left disabled by default; only enable for trusted authors |
| 3 | Resume-failed re-traverses the whole graph | Completed non-pure nodes re-execute | Use replay when side effects are not idempotent |
| 4 | Engine `_write_lock` is process-wide | Node status writes serialise across concurrent runs | Acceptable at current scale; needs per-execution sessions or Postgres |
| 5 | No inbound webhook triggers | The `webhook` node is outbound only | Trigger runs via the REST API |
| 6 | No image/TTS/STT provider ships by default | Those nodes fail with a clear `provider` error | Register a provider via `ai_orchestrator.register_*_provider` |
| 7 | AI traces are in-memory and bounded | Lost on restart | Use `GET /api/ai/usage` for durable token accounting |
| 8 | Backend coverage not re-measured in M4 | The 94% figure is stale (M2) | Re-run `pytest --cov` |
| 9 | Electron postinstall fails behind TLS-inspecting proxies | `npm install` errors on the Electron binary download | `npm install --ignore-scripts` (tests/build/typecheck all work) |
| 10 | `audit_events` table has no Alembic migration (**pre-existing**, predates M4) | The enterprise audit model is created by `create_all` but is absent from a migration-only deployment | Out of M4 scope; fix alongside the enterprise milestone |

---

## Historical


Status as of V1.1 Milestone 2 (2026-07-26).

## Resolved in V1.1

| Issue | Severity | Resolution |
| --- | --- | --- |
| `main` did not compile — `electron/main.ts` TS1005 syntax error | Critical | Fixed in M0 |
| Backend test suite was red (`test_settings_defaults`) | High | Fixed in M0 |
| `shell_command` node = unauthenticated arbitrary RCE | Critical | Disabled by default + allowlist + no shell + timeout (M1) |
| `http_request` node = SSRF to internal/metadata endpoints | Critical | Scheme/IP/DNS validation, timeouts, size caps (M1) |
| Workflow engine busy-waited at 100% CPU on blocked graphs | Critical | Event-driven scheduling (M1) |
| Parallel node writes corrupted results (`NULL identity key`) | Critical | Serialised engine persistence (M1) |
| Falsy node outputs (`{}`, `0`, `False`) were silently discarded | High | Only `None` now means "no output" (M1) |
| Cyclic graphs reported a misleading "deadlock" error | High | Explicit cycle detection with the offending path (M1) |
| Internal stack traces leaked to API clients | High | Sanitised error envelope (M1) |
| Event bus: one failing subscriber aborted the publish | High | Per-subscriber error isolation (M1) |
| Plugin hook failures silently swallowed by a bare `pass` | High | Logged, with per-hook success/failure results (M1) |
| `get_db()` leaked dirty sessions on exception | High | Rollback on exception (M1) |
| No per-node timeout — a hung node hung the workflow forever | High | Configurable timeout, default 300s (M1) |
| Unbounded node concurrency on wide fan-outs | High | `WORKFLOW_MAX_PARALLEL_NODES` semaphore (M1) |
| Build artifacts and the local DB were tracked in Git | Medium | Untracked, `.gitignore` hardened (M1) |
| AI conversation history was not context-window trimmed | High | Message/token-budget trimming added (M2) |
| Video/audio media processing was a mock `asyncio.sleep` | High | FFmpeg/ffprobe probe/poster pipeline with fallback added (M2) |
| Media asset `file_path` was not path-traversal validated | High | MEDIA_ROOT-only resolver blocks traversal, absolute paths, null bytes and symlink escapes (M2) |
| Media processing ran inside the HTTP request | High | Bounded background worker queue; process endpoint returns `202 Accepted` (M2) |

## Open

| Issue | Severity | Target |
| --- | --- | --- |
| No authentication/authorization on API endpoints | High | M7 |
| AI streaming (`generate_stream`) not implemented for OpenAI/Ollama | High | M4 |
| AI embeddings not implemented for OpenAI/Ollama | High | M4 |
| No workflow editor UI — the Workflows tab is static text | High | M3 |
| Zero frontend tests | High | M6 |
| Electron has no preload script, CSP or navigation guards | High | M6 |
| CI workflow cannot be auto-activated (see `ci/README.md`) | Low | Needs maintainer action |

## Environment notes

- **Electron binary download** is blocked by TLS interception in some sandboxed
  environments. Use `ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm install` to install
  dependencies; this is sufficient for `npm run build` and unit tests, but not
  for launching the desktop shell.
- **FFmpeg** is optional. M2 media processing degrades gracefully when
  `ffmpeg`/`ffprobe` are absent from `PATH`; image metadata and Pillow poster
  generation still work without FFmpeg.
