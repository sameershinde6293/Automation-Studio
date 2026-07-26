# Known Issues

## M4 (execution engine) — current

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
