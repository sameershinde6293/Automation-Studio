# Execution Engine (M4)

How Creator OS turns a saved workflow graph into a running, observable,
controllable execution.

- **Status:** implemented and tested in-process. Single-process asyncio only.
- **Audience:** contributors extending the engine or writing node types.
- **Companion docs:** [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md),
  [`M4_GAP_ANALYSIS.md`](M4_GAP_ANALYSIS.md).

---

## 1. Architecture

```
POST /api/workflows/{id}/executions
        │
        ▼
  ExecutionQueue  ──(priority, FIFO within band)──►  WorkerPool (N workers)
        │                                                   │
        │ QueueFullError → HTTP 429                          ▼
        │                                        WorkflowEngine.run_execution_v2
        │                                                   │
        │                    ┌──────────────────────────────┼───────────────────┐
        │                    ▼                              ▼                   ▼
        │             graph validation             node scheduling        ControlHandle
        │        (cycles, loop edges, caps)      (semaphore-bounded)   (pause/resume/stop)
        │                                                   │
        │                                                   ▼
        │                                        RuntimeNodeExecutor
        │                                   (schema → run → metrics → truncate)
        │                                                   │
        ▼                                                   ▼
  ExecutionBroker  ◄──── events + logs ───────────  persistence (batched)
        │
        ├──► SSE  GET /api/executions/{id}/stream
        ├──► poll GET /api/executions/{id}/events
        └──► DB   workflow_execution_logs
```

### Modules

| File | Responsibility |
| --- | --- |
| `services/workflow/graph.py` | Pure graph algorithms: cycles, topological order, layers, loop-edge splitting |
| `services/workflow/engine.py` | Scheduler, retries, branch gating, loops, control, persistence |
| `services/workflow/runtime.py` | `NodeContext`, schemas, metrics, error taxonomy, `RuntimeNodeExecutor` |
| `services/workflow/queue.py` | Priority queue + worker pool + admission control |
| `services/workflow/control.py` | `ControlHandle` / `ControlRegistry` — pause, resume, stop, cancel |
| `services/workflow/streaming.py` | Event fan-out, bounded subscriber queues, batched log writer |
| `services/workflow/history.py` | Search, replay, resume-failed, timelines, stats |
| `services/workflow/nodes/` | The 23-type node library |

---

## 2. Execution lifecycle

```
PENDING ──enqueue──► QUEUED ──worker──► RUNNING ──► COMPLETED
                        │                  │   └──► FAILED
                        │                  │   └──► CANCELLED
                        │                  ├─pause─► PAUSING ──► PAUSED ──resume──► RUNNING
                        │                  └─stop──► STOPPING ─► CANCELLED
                        └──cancel while queued──► CANCELLED
```

`ExecutionStatus.is_terminal` covers COMPLETED / FAILED / CANCELLED / SKIPPED.
`is_active` covers everything still holding a queue or worker slot.

### Two schedulers, deliberately

`run_execution` (M1) is **preserved verbatim** and still backs the legacy
`/{execution_id}/run` endpoint. `run_execution_v2` is the M4 scheduler and adds
branch gating, loops, pause/resume/stop, run variables, streaming and metrics.
Keeping both means the 825 pre-M4 tests continue to exercise the original code
path unchanged.

---

## 3. Scheduling

Nodes become runnable when every forward dependency has settled. Ready nodes are
dispatched concurrently up to `WORKFLOW_MAX_PARALLEL_NODES` (an
`asyncio.Semaphore`); the scheduler then waits on `FIRST_COMPLETED`.

Guards:

| Limit | Setting | Behaviour when exceeded |
| --- | --- | --- |
| Nodes per graph | `WORKFLOW_MAX_NODES` | Rejected at validation |
| Parallel nodes | `WORKFLOW_MAX_PARALLEL_NODES` | Queued behind the semaphore |
| Total node runs | `WORKFLOW_MAX_NODE_EXECUTIONS` | Run **fails** with an abort reason |
| Wall clock | `EXECUTION_TIMEOUT_SECONDS` | Run **fails** with an abort reason |
| Loop iterations | `WORKFLOW_MAX_LOOP_ITERATIONS` | Loop stops, run continues |

> Engine-level aborts (the last two) mark the run FAILED. An early M4 build
> reported COMPLETED because only node failures were inspected; that is fixed
> and covered by `test_node_execution_cap_is_enforced`.

---

## 4. Conditional branching

Before M4 the engine ignored branch results entirely and executed **both**
sides of every condition. Now:

1. A `condition` node returns `{"result": bool, "branch": "true"|"false"}` and
   declares `NodeResult.branches`.
2. The engine records that decision.
3. Outgoing edges whose **label** does not match are suppressed.
4. Suppression cascades to descendants that lose all inbound paths.

```
        ┌── label "true" ──► Send Email ──┐
Check ──┤                                 ├──► Done
        └── label "false" ─► Log Skip  ───┘
```

- Unlabelled edges are **always** followed — a condition with unlabelled
  outputs gates nothing.
- A join node reachable through the taken branch still runs.
- Labels come from React Flow's `sourceHandle`, mapped by `graphAdapter.ts`.

---

## 5. Loops

A cycle is legal **only** when its closing edge is labelled `loop`
(`LOOP_EDGE_PREFIX`). `validate_graph_with_loops` removes those edges before
cycle detection, so the schedulable graph stays a DAG; any other cycle is still
an error.

Per iteration the engine recomputes the loop body (`loop_body()`), resets those
nodes to PENDING with an incremented `iteration`, and re-runs them.

Termination — whichever comes first:
- the node closing the loop is a condition evaluating false;
- the per-node `max_iterations` is reached;
- `WORKFLOW_MAX_LOOP_ITERATIONS` is reached.

The `loop` **node** is separate and self-contained: it iterates a collection in
one execution (`collect` mode) and needs no back-edge.

---

## 6. Node runtime

Every node implements `RuntimeNodeExecutor.run(node, context, config)`. The base
class validates config against the schema, runs the node, truncates the output
and records metrics.

```python
class MyNode(RuntimeNodeExecutor):
    label = "My Node"
    category = "custom"
    aliases = ("my_node",)
    schema = NodeSchema(
        inputs=[FieldSpec("url", "string", required=True)],
        outputs=[FieldSpec("body", "string")],
    )

    async def run(self, node, context, config):
        return {"body": await fetch(config["url"])}
```

`RuntimeNodeExecutor` is registered as a **virtual subclass** of
`BaseNodeExecutor` (`BaseNodeExecutor.register(...)`) rather than inheriting
directly — direct inheritance created a circular import between `runtime` and
`executors`.

### Field coercion

Every HTML input yields a string, so `FieldSpec` coerces `"2.5"`→`2.5`,
`"true"`→`True`, `'{"a":1}'`→`dict`, `"x, y"`→`["x","y"]`. Unknown config keys
pass through untouched so editor-only metadata never breaks a saved graph.

### Error taxonomy and retries

| Code | Retryable | Typical source |
| --- | --- | --- |
| `validation`, `permission`, `not_found`, `disabled`, `cancelled` | **No** | Bad config, blocked URL, feature off |
| `timeout`, `network`, `provider`, `rate_limit`, `runtime`, `unknown` | Yes | Transient failures |

Non-retryable failures abort immediately instead of burning the retry budget —
a misconfigured node fails in one attempt, not three.

### Context

`NodeContext` subclasses `dict` (so M1 executors and `resolve_reference` keep
working) and adds reserved keys:

| Key | Contents |
| --- | --- |
| `vars` | Run variables, seeded from `input_data` |
| `loop` | Innermost loop frame: `{item, index, total}` |
| `run` | `{execution_id, workflow_id}` |

Node outputs are addressable by id and by name: `{{ 3.result }}`,
`{{ Fetch.response.items.0 }}`. Reserved keys are never shadowed by a node name.

---

## 7. Node library (23 types)

| Category | Nodes |
| --- | --- |
| Control | `start`, `end`, `variable`, `condition`, `loop`, `delay` |
| AI | `prompt`, `aiChat`, `aiCompletion`, `imageGeneration` |
| Network | `httpRequest`, `webhook` |
| Script/data | `python`, `javascript`, `database` |
| Integration/IO | `email`, `file`, `folder` |
| Media | `tts`, `stt`, `ffmpeg`, `mediaProcessing` |

Each registers snake_case aliases (83 names total). Pre-existing M1 types
(`http_request`, `delay`, `template`, …) keep their original executors so saved
V1.0/V1.1 workflows are unaffected.

### Security posture — read before enabling

| Node | Default | Reality |
| --- | --- | --- |
| `python` | **Disabled** | Restricted `exec` (no imports/builtins/file access). **Not a sandbox** — restricted-`exec` is escapable. |
| `javascript` | **Disabled** | Shells out to Node.js with a timeout. **No isolation.** |
| `database` | **Disabled** | Raw SQL; read-only unless `allow_write`; stacked statements rejected. |
| `httpRequest` / `webhook` | Enabled | Full M1 SSRF protection (scheme allowlist, private/loopback/metadata IP blocking incl. DNS, redirect cap, size cap). |
| `file` / `folder` | Enabled | Confined to `MEDIA_ROOT` via `resolve_media_path`. |
| `ffmpeg` | Enabled | Never accepts a raw command line; paths resolved in `MEDIA_ROOT`, path-like `extra_args` rejected. |
| `email` | Enabled | **Dry-run** when `SMTP_HOST` is unset — reports `sent: false` rather than faking delivery. |
| `imageGeneration` / `tts` / `stt` | Enabled | Fail with a clear `provider` error when no provider is registered. Creator OS ships none. |

Only enable the script and database nodes where every workflow author is
already trusted with local code execution.

---

## 8. Execution control

| Action | Semantics |
| --- | --- |
| **Pause** | Stop scheduling new nodes; in-flight nodes finish; park until resumed |
| **Resume** | Continue scheduling |
| **Stop** | Graceful: drain in-flight nodes, then terminate as CANCELLED |
| **Cancel** | Hard: cancel the task; in-flight nodes receive `CancelledError` |

Requests are thread-safe (`request_pause()` etc. are callable from FastAPI's
threadpool) while the waiting side is loop-affine. `asyncio.Event` is not
thread-safe, so mutations go through a `threading.Lock` and events are set via
`call_soon_threadsafe`. Cancelling a still-queued run removes it from the queue
without ever executing it.

---

## 9. Queue and workers

- Ordered by `(priority, sequence)` — lower priority value first, FIFO within a
  band. Priorities: `CRITICAL=0`, `HIGH=10`, `NORMAL=50`, `LOW=90`.
- `EXECUTION_QUEUE_MAX_SIZE` caps waiting runs; over capacity raises
  `QueueFullError` → **HTTP 429**.
- `EXECUTION_MAX_WORKERS` coroutines consume the queue.
- Workers start from the **application lifespan**, not lazily from a request.
  Creating long-lived tasks inside a request scope leaked them into that
  request's lifetime and hung sync test clients.
- When no pool is running (embedded host / tests that skip the lifespan),
  `enqueue()` submits directly and reports `mode: "direct"` with status
  `PENDING`, preserving the pre-M4 response contract.

---

## 10. Real-time updates

`ExecutionBroker` fans events out per execution.

- **Bounded subscriber queues** (`EXECUTION_STREAM_QUEUE_SIZE`, drop-oldest): a
  slow SSE client is degraded, never allowed to stall the engine.
- **Replay buffer** so a reconnecting client can resume from `after_sequence`.
- **Batched log writes** (`EXECUTION_LOG_BATCH_SIZE`, flush interval, immediate
  flush on ERROR) instead of one transaction per line.

Events: `execution.queued|started|progress|paused|resumed|stopping|finished`,
`node.started|finished|retry|skipped`, `log`.

The SSE endpoint terminates on a live terminal event, on a **replayed** terminal
event, and synthesises one when the run finished long ago and its buffer was
evicted. Missing the middle case made the endpoint heartbeat forever.

M1's `workflow.*` events are still published on the global event bus for
backwards compatibility.

---

## 11. History, replay and resume

- `GET /api/executions` — filter by workflow, status, trigger, date; search
  matches workflow name **or** error text.
- **Replay** — a fresh run of the same graph, inheriting inputs, linked via
  `parent_execution_id`.
- **Resume-failed** — only for FAILED/CANCELLED runs. Completed node outputs are
  seeded into the new run's `input_data.__resume__`.

> **Honest limitation.** Resume-failed is a *retry with prior context*, not true
> mid-graph resumption: the engine still traverses the whole graph, so
> already-completed nodes re-execute unless they are pure. Proper checkpoint
> resumption is on the roadmap.

---

## 12. AI orchestration

- **Fallback chain** (`AI_FALLBACK_CHAIN`); a pinned provider is tried first.
- **Circuit breaker** — finally uses the `AI_CIRCUIT_BREAKER_*` settings that
  existed since M1 but were never wired up. CLOSED → OPEN after N consecutive
  failures → HALF_OPEN probe after the cooldown.
- **Cost model** — list-price defaults per model, overridable. Estimates, not
  billing truth.
- **Tracing** — bounded in-memory ring buffer of recent calls including every
  fallback attempt. Lost on restart.

---

## 13. Performance notes

Addressed in M4:

| Issue | Before | Now |
| --- | --- | --- |
| Node writes | 4 transactions per node | 1 per outcome |
| Graph load | 4 sessions | 1 (`_fetch_execution_plan`) |
| Log writes | 1 transaction per line | Batched |
| History queries | Unindexed | `(workflow_id, status)`, `(status, priority, id)` |
| Admission | Unbounded `create_task` | Bounded queue + 429 |
| Large outputs | Unbounded | Truncated at `EXECUTION_MAX_OUTPUT_BYTES` |

**Still outstanding:** the engine's `_write_lock` is a single process-wide
`threading.Lock`. It is now held for far less time, but it still serialises node
status writes across *all* concurrent executions. Removing it requires
per-execution sessions or Postgres.

---

## 14. Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `EXECUTION_MAX_WORKERS` | 4 | Concurrent executions |
| `EXECUTION_QUEUE_MAX_SIZE` | 1000 | Queue capacity (429 beyond) |
| `EXECUTION_TIMEOUT_SECONDS` | 3600 | Whole-run wall clock |
| `WORKFLOW_MAX_PARALLEL_NODES` | 8 | Concurrent nodes per run |
| `WORKFLOW_MAX_LOOP_ITERATIONS` | 1000 | Loop guard |
| `WORKFLOW_MAX_NODE_EXECUTIONS` | 10000 | Total node runs per execution |
| `EXECUTION_LOG_BATCH_SIZE` | 25 | Log rows per flush |
| `EXECUTION_STREAM_QUEUE_SIZE` | 256 | Per-subscriber buffer |
| `EXECUTION_MAX_OUTPUT_BYTES` | 262144 | Node output cap |
| `ALLOW_PYTHON_EXECUTOR` | false | Enable the Python node |
| `ALLOW_JAVASCRIPT_EXECUTOR` | false | Enable the JavaScript node |
| `ALLOW_DATABASE_EXECUTOR` | false | Enable the Database node |
| `AI_FALLBACK_CHAIN` | openai,local,mock | Provider order |

---

## 15. Known limitations

1. Single-process only — no distributed workers; the queue is in-memory and is
   lost on restart (queued rows survive in the database but are not re-claimed
   automatically).
2. `python` / `javascript` are restricted interpreters, **not** sandboxes.
3. Resume-failed re-traverses the graph (see §11).
4. The global `_write_lock` still serialises status writes.
5. No inbound webhook triggers — the `webhook` node is outbound only.
6. No image/TTS/STT providers ship with Creator OS; those nodes need one
   registered.
7. AI traces are in-memory and are lost on restart.
