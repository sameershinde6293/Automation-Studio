# Known Issues

## M5 (production hardening) — current

| # | Issue | Impact | Workaround |
| --- | --- | --- | --- |
| 1 | Single-process execution; the priority queue is in-memory | Queued runs are lost on restart (rows persist as QUEUED but are never re-claimed). Running >1 replica risks **double execution** of the same row | Keep `WEB_CONCURRENCY=1` and one backend replica; re-trigger runs after a restart |
| 2 | Rate limiting is per-process and in-memory | With N processes the effective limit is N× the configured value | Single process, or terminate rate limiting at a proxy |
| 3 | SSE broker is per-process | A client connected to replica A sees no events from executions on replica B | Single replica |
| 4 | Python sandbox is **not a security boundary** | A CPython escape yields the backend user's OS privileges. `os.stat` raises no audit event in 3.11, so file metadata is readable post-escape | Left disabled by default; run untrusted code in a container with seccomp. See `SECURITY.md` §5 |
| 5 | The JavaScript node is **not sandboxed at all** | Enabling it grants local code execution with the backend user's permissions | Left disabled by default; only enable for trusted authors |
| 6 | RBAC is global, not per-resource | Any `editor` can modify any workflow; no ownership, ACLs or tenancy | Do not use a single instance to isolate mutually untrusting users |
| 7 | Access tokens cannot be revoked before expiry | A stolen access token stays valid for up to `AUTH_ACCESS_TOKEN_TTL_SECONDS` (default 15 min). Only refresh sessions are stateful | Shorten the TTL; deactivating a user kills refresh but not the live access token |
| 8 | No MFA, SSO/OIDC or password reset flow | An administrator must reset forgotten passwords | — |
| 9 | Audit coverage is partial and not tamper-evident | Auth and user administration are audited; workflow/media mutations are not. `POST /api/enterprise/audit` still accepts a caller-supplied `user_id` | Do not treat self-reported audit entries as trustworthy attribution |
| 10 | Deployment assets have never been executed | Dockerfiles, compose stack and the documented procedures are unverified — no container runtime was available during M5 | Treat the first deployment as a validation exercise |
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
