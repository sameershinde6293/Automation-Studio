# Known Issues

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
