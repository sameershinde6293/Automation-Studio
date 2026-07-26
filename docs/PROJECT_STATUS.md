# Project Status

**Current phase:** Version 1.1 development
**Version:** 1.1.0 (in progress)
**Last updated:** 2026-07-26

## Milestone progress

| # | Milestone | Status |
| --- | --- | --- |
| M0 | Repair & hygiene | ✅ Complete |
| M1 | Backend core hardening | ✅ Complete |
| M2 | API expansion & service completion | ✅ Complete (merged, PR #3) |
| M3 | Drag-and-drop Workflow Editor | ✅ Complete (merged, PR #4) |
| M4 | Execution engine & AI orchestration | ✅ Complete (this branch) |
| M5 | Advanced Media Pipeline UX | ⬜ Planned |
| M6 | UI/UX polish + accessibility | ⬜ Planned |
| M7 | Performance, security, docs, CI | ⬜ Planned |

## Health

| Metric | V1.0 (as found) | Now |
| --- | --- | --- |
| Backend build | ✅ | ✅ |
| Frontend build | ❌ broken (TS1005) | ✅ |
| Backend tests | 19 passed / **1 failed** | **1085 passed / 0 failed** |
| Backend coverage | 82% | 94% (M2 measurement; not re-measured in M4) |
| Frontend tests | none | **105 passed / 0 failed** |
| Frontend typecheck | ❌ broken | ✅ `tsc --noEmit` clean |
| Alembic migrations | not exercised | ✅ M0–M4 migrations present (M4: `c4e7a1b90d52`) |
| Critical security issues | 4 open | **0 open** |

## Recent work (M4)

Turned the visual editor into an executable platform. The execution engine gained
conditional branch gating, bounded loops, pause/resume/graceful stop, a bounded
priority queue with a worker pool, and per-node metrics. A unified node runtime
with declarative schemas now backs all 23 editor node types — before M4 the
editor's palette and the backend registry intersected on `{delay}` only, so
**no editor-built workflow could be saved or run**. Real-time feedback arrives
over SSE with bounded subscriber queues and durable, batched execution logs. AI
orchestration gained provider fallback, a working circuit breaker, cost
estimation and tracing. The frontend gained execution controls, live node state,
a streaming log viewer and a history panel with replay.

Frontend testing existed only on paper before M4: five vitest files were
committed in M3 with no runner, no dependencies and no `test` script. Wiring up
vitest immediately exposed two real M3 defects (undo/redo never worked; the
BaseNode config test asserted nothing about the store).

### M4 verification

| Check | Result |
| --- | --- |
| Backend tests | 1085 passed (825 pre-existing + 260 new) |
| Frontend tests | 105 passed (was 0 runnable) |
| Frontend typecheck | `tsc --noEmit` clean |
| Frontend build | `vite build` succeeds |
| Pre-existing tests modified | none |

### M4 known limitations

- Single-process execution only; the in-memory queue does not survive restart.
- `python`/`javascript` nodes are restricted interpreters, **not** sandboxes,
  and ship disabled.
- Resume-failed re-traverses the graph rather than resuming mid-graph.
- The engine's global write lock still serialises node status writes.
- No inbound webhook triggers; no image/TTS/STT provider ships by default.
- Backend coverage was not re-measured in M4; the 94% figure is from M2.

## Previous work (M2)

Completed the M2 service layer for the AI runtime and media system. AI now has
conversation CRUD, message endpoints, model registry CRUD, provider
introspection, validated chat completions, context trimming and token usage
reporting. Media now has secure asset CRUD constrained to `MEDIA_ROOT`, streaming
upload enforcement, content-based MIME detection, secure download/delete, a
bounded background processing pool, progress-reporting jobs, `202 Accepted`
processing semantics with optional `wait=true`, and FFmpeg/ffprobe integration
with graceful fallback.

## Previous work (M1)

Fixed two critical security holes (shell RCE, HTTP SSRF) and three critical
correctness bugs (engine busy-wait, parallel-write data corruption, discarded
falsy outputs). Added a typed error hierarchy, structured logging with secret
redaction, request correlation, security middleware, rate limiting, SQLite WAL
tuning, indices on all hot foreign keys, six new node types with inter-node
templating, and the complete node/edge/execution CRUD surface the V1.1 visual
editor will persist against.

## Estimated overall completion

**≈ 80%** toward a polished commercial-grade Creator OS.

The core loop — design a workflow, run it, watch it, control it, inspect and
replay it — is complete and tested. Remaining work is breadth (media UX,
first-party AI/speech providers, inbound triggers), operational hardening
(multi-process execution, durable queue, true mid-graph resume) and polish
(accessibility, packaging, CI).
