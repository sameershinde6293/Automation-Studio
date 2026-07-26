# M4 Gap Analysis — Execution Engine & AI Orchestration

**Audit date:** 2026-07-26
**Baseline commit:** `f44df82` (main, after PR #4 / M3 merge)
**Branch:** `arena/019f9e62-automation-studio`
**Auditor scope:** workflow engine, graph algorithms, node executors, AI orchestrator,
media pipeline, event bus, scheduler, workflow REST API, workflow editor integration.

**Verification performed before writing this document:**

| Check | Result |
| --- | --- |
| PR #4 merged into `main` | ✅ `gh pr list` shows PR #4 `MERGED`; local `HEAD` = `f44df82` = merge commit |
| Backend test baseline | ✅ **825 passed, 0 failed** (`pytest`, 12.3s) |
| Backend deps installable | ✅ `requirements.txt` installs cleanly (Python 3.11.2) |
| Frontend test runner | ❌ **Not configured** — see §6 |

---

## 1. Completed execution features (verified present and working)

These are genuinely implemented and covered by the existing 825-test suite. **M4 must
not replace them.**

### Graph layer — `app/services/workflow/graph.py` (247 LOC)
- `build_adjacency` — dependency/dependent maps.
- `find_cycles` — **iterative** DFS (no stack overflow on deep graphs), rotation-invariant
  cycle de-duplication.
- `topological_order` — Kahn's algorithm, deterministic ordering via `key=str`.
- `execution_layers` — parallel-executable layer grouping.
- `descendants` — reachability, used for branch skipping.
- `validate_graph` — node cap, duplicate ids, dangling edge refs, self-loops, cycles,
  orphan warnings.

### Engine layer — `app/services/workflow/engine.py` (652 LOC)
- Event-driven async scheduler (the V1.0 busy-wait bug is fixed).
- Bounded concurrency via `asyncio.Semaphore(WORKFLOW_MAX_PARALLEL_NODES)`.
- Per-node timeouts (`asyncio.wait_for`).
- Retry with exponential backoff, per-node `retry_policy` override.
- `on_error` policies: `fail` / `continue` / `skip_branch`.
- Cancellation via `asyncio.Task.cancel()` + `CancelledError` propagation.
- Checkpointing of `completed`/`failed`/`skipped` into `WorkflowExecution.state`.
- `_NodeSnapshot` detachment — avoids `DetachedInstanceError`, keeps sessions short.
- Serialised status writes via `threading.Lock` (fixes the SQLite parallel-write
  corruption bug).
- Falsy outputs (`{}`, `0`, `False`) correctly persisted.
- Graceful `shutdown()` wired into the FastAPI lifespan.

### Node executors — `app/services/workflow/executors.py` (659 LOC)
10 registered types: `dummy`, `noop`, `delay`, `math_add`, `math_expression`,
`template`, `transform`, `branch`, `http_request`, `shell_command`.
- SSRF hardening on `http_request` (scheme allowlist, private/loopback/link-local/
  metadata-IP blocking incl. DNS resolution, redirect cap, response-size cap).
- `shell_command` disabled by default, allowlist-gated, no shell, process-kill on timeout.
- `{{ ref }}` template interpolation with dotted-path resolution (`resolve_reference`).
- Plugin-SDK registration hooks (`register` / `unregister` / `catalog`).

### Persistence
- `Workflow`, `Node`, `Edge`, `WorkflowExecution`, `NodeExecution` models.
- Indices on every hot FK (migration `a1b2c3d4e5f6`).
- `ExecutionStatus` enum with `is_terminal`.

### API — `app/api/routers/workflow_router.py` (483 LOC)
Workflow CRUD, node CRUD, edge CRUD (with cycle-prevention on insert), full-graph
GET/PUT, `POST /validate`, `POST /{id}/executions`, `GET /executions/{id}`,
`POST /executions/{id}/cancel`, plus the deprecated V1.0 `/{execution_id}/run`.

### Supporting infrastructure
- Event bus with wildcard subscribers, async-subscriber support, error isolation and a
  200-entry ring buffer (`GET /api/system/events`).
- APScheduler wrapper with SQLAlchemy job store.
- AI orchestrator: conversation-backed chat, context trimming, token accounting rows.
- Media pipeline: bounded `ThreadPoolExecutor`, job progress, ffprobe/ffmpeg with
  graceful fallback.

---

## 2. Missing runtime features

| # | Feature | Current state | Severity |
| --- | --- | --- | --- |
| R1 | **Pause / Resume** | `ExecutionStatus.PAUSED` exists in the enum but **nothing ever sets it and nothing honours it**. No pause primitive in the engine. | 🔴 Critical |
| R2 | **Stop (graceful)** | Only hard `cancel()` exists. No "let in-flight nodes finish, then stop". | 🟠 High |
| R3 | **Conditional branching** | `BranchExecutor` computes `{"branch": "true"/"false"}` and `Edge.label` exists — but **the engine never reads either**. Both branches always execute. Conditional logic is effectively non-functional. | 🔴 Critical |
| R4 | **Loops** | No loop node, no iteration, no back-edges. `validate_graph` rejects any cycle outright. | 🔴 Critical |
| R5 | **Queue management** | `submit()` calls `asyncio.create_task` immediately. Unbounded — 1000 concurrent runs would spawn 1000 tasks. No queue, no admission control. | 🔴 Critical |
| R6 | **Worker scheduling** | No worker pool for executions (only intra-execution node concurrency). | 🟠 High |
| R7 | **Execution priorities** | No priority concept anywhere. | 🟠 High |
| R8 | **Replay execution** | Not implemented. | 🟠 High |
| R9 | **Resume failed execution** | Checkpoint data is written but never read back. No resume path. | 🟠 High |
| R10 | **Node input/output schema** | `Node.input_schema` / `output_schema` columns exist and are **completely unused** — no validation at runtime. | 🟠 High |
| R11 | **Per-node execution metrics** | Only `duration_ms`. No attempt timeline, no queue-wait time, no token/cost attribution. | 🟡 Medium |
| R12 | **Structured execution logs** | Logs go to the Python logger only. Nothing persisted, nothing streamable per-execution. | 🔴 Critical |
| R13 | **Variable / context store** | Context is an in-memory dict keyed by node id/name. No first-class workflow variables, no typed inputs to a run. | 🟠 High |

---

## 3. Execution bottlenecks

| # | Bottleneck | Evidence | Impact |
| --- | --- | --- | --- |
| B1 | **Global write lock** | `WorkflowEngine._write_lock` is a single `threading.Lock` on the singleton, held across a full DB transaction. | Serialises node-status writes across **all** concurrent executions, not just one. Throughput ceiling ≈ 1 write at a time process-wide. |
| B2 | **Session churn** | Every `_update_node_status` / `_checkpoint` opens a new `SessionLocal()`. | ~4 sessions per node (RUNNING → retry → COMPLETED → checkpoint). A 100-node graph ≈ 400 sessions. |
| B3 | **Checkpoint per completion** | `_checkpoint` runs after *every* settled task. | O(N) extra full-row JSON writes per execution. |
| B4 | **Get-or-create per status write** | `_update_node_status` does a SELECT then maybe INSERT on every call. | Doubles query count; the reason B1's lock exists at all. |
| B5 | **No batching** | Each node status change is its own transaction. | High fsync pressure on SQLite. |
| B6 | **Unbounded task creation** | `submit()` has no admission control. | Memory/CPU blow-up under load; no back-pressure. |
| B7 | **Execution history queries** | `get_by_workflow` only. No global list, no status filter, no date filter, no text search. UI would have to fetch-all-and-filter. | O(N) transfer for history views. |
| B8 | **Event history is in-process** | `event_bus._history` is a 200-entry `deque` shared by *all* events. | Execution events are evicted quickly; unusable as a per-execution log source. |

---

## 4. Missing integrations

| # | Integration | Finding | Severity |
| --- | --- | --- | --- |
| I1 | **Editor ↔ backend node types** | Editor palette exposes 22 types (`start`, `end`, `aiChat`, `aiCompletion`, `prompt`, `variable`, `condition`, `loop`, `delay`, `httpRequest`, `webhook`, `python`, `javascript`, `database`, `email`, `file`, `folder`, `imageGeneration`, `tts`, `stt`, `ffmpeg`, `mediaProcessing`). Backend registry has 10 (`dummy`, `noop`, `delay`, `math_add`, `math_expression`, `template`, `transform`, `branch`, `http_request`, `shell_command`). **Intersection = `{delay}` only.** `PUT /graph` rejects unknown types → **saving any editor-built workflow returns HTTP 422**. | 🔴 Critical |
| I2 | **Graph save payload shape** | Frontend `saveWorkflow()` POSTs `{name, nodes:[{id,type,position,data}], edges:[{id,source,target}]}`. Backend `GraphPayload` expects `{nodes:[{name,node_type,position_x,position_y,config}], edges:[{source_id,target_id}]}`. Frontend ids are UUID **strings**; backend expects **ints**. **The two schemas are mutually incompatible.** | 🔴 Critical |
| I3 | **AI runtime ↔ workflow engine** | Zero AI node executors. A workflow cannot invoke `ai_orchestrator`. The AI runtime is API-only. | 🔴 Critical |
| I4 | **Media pipeline ↔ workflow engine** | Zero media node executors. Workflows cannot call FFmpeg, TTS, STT or the media pipeline. | 🔴 Critical |
| I5 | **Live execution transport** | No WebSocket, no SSE. `GET /api/system/events` is poll-only and globally shared. `ExecutionPanel.tsx` contains a literal placeholder: `// Simulate live execution animation (for demo until real backend execution stream)` with an empty `setInterval` body. | 🔴 Critical |
| I6 | **Run from editor** | There is **no run button anywhere in the frontend**. `ExecutionPanel` only renders `executionStates`, which nothing ever populates. | 🔴 Critical |
| I7 | **Provider fallback** | `AIOrchestrator.chat` resolves exactly one provider and raises `ProviderError` on failure. No fallback chain, no circuit breaker (settings `AI_CIRCUIT_BREAKER_*` exist but are **unused**). | 🟠 High |
| I8 | **Cost estimation** | `TokenUsage` records tokens but there is **no pricing model and no cost field**. | 🟠 High |

---

## 5. Missing APIs

Audited against the full router surface (`workflow`, `ai`, `media`, `plugin`,
`project`, `enterprise`, `system`). The following do **not** exist:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/executions/{id}/pause` | Pause a running execution |
| `POST` | `/api/executions/{id}/resume` | Resume a paused execution |
| `POST` | `/api/executions/{id}/stop` | Graceful stop |
| `POST` | `/api/executions/{id}/replay` | Re-run the same graph as a new execution |
| `POST` | `/api/executions/{id}/resume-failed` | Retry only failed/pending nodes |
| `GET` | `/api/executions` | Global list + search + filter (status, workflow, trigger, date, text) |
| `GET` | `/api/executions/{id}/logs` | Structured per-execution log records |
| `GET` | `/api/executions/{id}/stream` | **SSE live event stream** |
| `GET` | `/api/executions/{id}/timeline` | Node-by-node timing/Gantt data |
| `GET` | `/api/executions/queue` | Queue depth, running set, worker status |
| `GET` | `/api/executions/stats` | Aggregate success rate / durations |
| `GET` | `/api/system/node-schemas` | Input/output schemas per node type (only a partial `config_schema` catalog exists today) |
| `POST` | `/api/ai/estimate` | Token + cost estimation |
| `GET` | `/api/ai/traces` | AI execution traces |

**Already present — must NOT be duplicated:** workflow CRUD, node CRUD, edge CRUD,
`GET/PUT /graph`, `POST /validate`, `POST /{workflow_id}/executions`,
`GET /workflows/executions/{id}`, `POST /workflows/executions/{id}/cancel`,
`GET /system/node-types`, `GET /system/events`, `GET /system/metrics`, all AI and
media endpoints.

---

## 6. Testing gaps

### Backend (825 tests passing — good coverage of what exists)
- ❌ No pause/resume tests (feature absent).
- ❌ No queue/priority/worker tests (feature absent).
- ❌ No conditional-branch-gating tests (feature absent).
- ❌ No loop tests (feature absent).
- ❌ No replay / resume-failed tests (feature absent).
- ❌ No execution-history search/filter tests (feature absent).
- ❌ No streaming/SSE tests (feature absent).
- ❌ No AI-node or media-node executor tests (executors absent).
- ❌ No provider-fallback or cost-estimation tests (feature absent).
- ⚠️ Engine tests rely on `WorkflowEngine` being re-instantiable; the new queue/worker
  layer must preserve this to avoid breaking all 825.

### Frontend — **no runnable tests at all**
- `frontend/package.json` has **no `test` script**, and **no `vitest`,
  `@testing-library/react`, `jsdom` or `@testing-library/jest-dom` dependency**.
- Five test files exist (`baseNode`, `canvas`, `clipboard`, `importExport`,
  `workflowStore`) and all `import { describe, it, expect } from 'vitest'` — they are
  **dead code that cannot execute**.
- `frontend/node_modules` is not installed in this environment.
- No `vitest.config.ts`, no test setup file, no `jsdom` environment.
- **Conclusion: M3's claimed frontend tests are unverified.** M4 must add the runner.

---

## 7. Performance concerns

| # | Concern | Detail |
| --- | --- | --- |
| P1 | Process-wide write serialisation | B1 — a single lock throttles every execution's persistence. |
| P2 | Session-per-write | B2/B4 — 4× round trips per node. |
| P3 | No admission control | B6 — nothing prevents thousands of simultaneous executions. |
| P4 | Unindexed history queries | No composite index on `(workflow_id, status)` or `created_at` for the history panel's filter paths. |
| P5 | JSON column growth | `WorkflowExecution.state` is rewritten in full on every checkpoint; grows linearly with node count. |
| P6 | Event bus eviction | B8 — 200-entry global ring buffer makes per-execution log replay impossible. |
| P7 | Node results held in memory | `context` accumulates every node's full output for the entire run; a 1000-node graph with large HTTP responses is unbounded. |
| P8 | No streaming back-pressure | Any future SSE implementation needs bounded per-subscriber queues or a slow client stalls the engine. |

---

## 8. M4 implementation plan (derived from the gaps above)

Ordered by dependency. Each item maps to gap ids.

1. **Persistence & migration** — `ExecutionLog` table, execution `priority` / `queued_at` /
   `parent_execution_id` / `metrics`, `NodeExecution.attempt_metrics`, composite indices. *(R12, B7, P4)*
2. **Unified node runtime** — `NodeSpec` with declared input/output schemas, validation,
   metrics and error classification, layered over the existing `BaseNodeExecutor`. *(R10, R11)*
3. **Full node library** — all 23 editor node types, incl. AI, media, script, data and
   IO nodes, registered with editor-compatible names plus aliases. *(I1, I3, I4)*
4. **Execution control** — pause/resume/stop signal primitives honoured by the scheduler. *(R1, R2)*
5. **Branch gating + loops** — edge-label-driven activation and bounded back-edge loops. *(R3, R4)*
6. **Queue + workers + priorities** — bounded priority queue with a worker pool. *(R5, R6, R7, B6, P3)*
7. **Persistence optimisation** — per-execution session + batched writes, replacing the
   global lock. *(B1–B5, P1, P2)*
8. **Execution history service** — search/filter/replay/resume-failed. *(R8, R9, B7)*
9. **Streaming** — bounded-queue SSE broker + per-execution log buffer. *(I5, B8, P8)*
10. **AI orchestration v2** — fallback chain, circuit breaker, templating, memory,
    cost model, tracing. *(I7, I8)*
11. **Execution API router** — only the 14 missing endpoints from §5.
12. **Frontend** — graph payload adapter (fixes I2), run/pause/resume/stop controls,
    SSE-driven live state, log viewer, progress, history panel. *(I2, I6)*
13. **Frontend test infrastructure** — vitest + jsdom + testing-library, so §6's dead
    tests run and new M4 tests run. *(§6)*
14. **Documentation** — `EXECUTION_ENGINE.md`, CHANGELOG, PROJECT_STATUS, API docs,
    architecture.

### Explicit non-goals for M4
- Distributed/multi-process workers (single-process asyncio only).
- Real sandboxing for `python`/`javascript` nodes — these will be **restricted
  interpreters**, not secure sandboxes, and will be documented as such and
  disabled by default.
- Replacing SQLite with Postgres.
