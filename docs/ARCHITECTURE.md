# Architecture

Creator OS v1.1 · last updated 2026-07-26 (M6)

> **M6 measured characteristics.** The architecture below is unchanged by M6,
> but its runtime behaviour has now been measured rather than assumed:
>
> * **Request concurrency is bounded by database-pool capacity**, not CPU.
>   Every in-flight request holds a connection for its whole handler lifetime,
>   so capacity (`DB_POOL_SIZE + DB_MAX_OVERFLOW`, default 80) is the real
>   concurrency ceiling — roughly 100 concurrent authenticated requests per
>   instance at 0% error. Beyond it, load is shed as `503` + `Retry-After`.
> * **The process is single-instance by design.** The execution queue, rate
>   limiter, SSE broker and error aggregator all hold state in process memory.
>   Running multiple workers multiplies the rate limit (measured: 3x at four
>   workers) and gives each worker its own queue. The APScheduler jobstore is
>   the one shared-state component and is safe across processes.
> * **Recovery is automatic.** With `pool_pre_ping` and the readiness gate, a
>   database outage takes the instance out of rotation and it returns ~1 s
>   after the database comes back, with no restart.
>
> See `M6_VALIDATION_REPORT.md` for the measurements and
> `KNOWN_ISSUES.md` for the scaling limits this implies.

---

## 1. What Creator OS is

A local-first automation platform: users build workflows on a visual canvas,
and an execution engine runs them as directed graphs of typed nodes with AI,
media, network, scripting and control-flow capabilities.

It ships in two shapes from one codebase:

- a **desktop application** (Electron shell + FastAPI backend on localhost),
- a **server deployment** (containers, PostgreSQL, authentication enabled).

The difference is configuration, not code — see `AUTH_ENABLED` in
`SECURITY.md`.

---

## 2. Component map

```
┌──────────────────────────────────────────────────────────────────┐
│ Frontend — React 18 + TypeScript + Vite                          │
│                                                                  │
│   App shell (ARIA tabs, error boundaries, health)                │
│     └── WorkflowEditor                                           │
│           ├── NodePalette      drag source                       │
│           ├── WorkflowCanvas   React Flow, 22 node components    │
│           ├── PropertiesPanel  schema-driven config              │
│           └── ExecutionPanel   controls · progress · logs        │
│                                                                  │
│   State: zustand — workflowStore (graph) · executionStore (runs) │
│   Transport: REST + Server-Sent Events                           │
└───────────────────────────────┬──────────────────────────────────┘
                                │ HTTP / SSE
┌───────────────────────────────▼──────────────────────────────────┐
│ Backend — FastAPI (Python 3.11)                                  │
│                                                                  │
│  Middleware (outermost first)                                    │
│    RequestContext → CORS → GZip → TrustedHost → SecurityHeaders  │
│    → BodySizeLimit → RateLimit → CSRF                            │
│                                                                  │
│  API  /api/… and /api/v1/…                                       │
│    auth · projects · workflows · executions · ai · media         │
│    plugins · enterprise · system                                 │
│                                                                  │
│  Services                                                        │
│    security   principal · tokens · passwords · sandbox           │
│    workflow   engine · queue · graph · runtime · nodes ·         │
│               control · history · streaming                      │
│    ai         orchestrator · providers (openai/local/mock)       │
│    media      pipeline · ffmpeg · storage                        │
│                                                                  │
│  Domain      models · repositories                               │
│  Infra       database · logging · events · scheduler ·           │
│              observability (metrics · error aggregation)         │
└───────────────────────────────┬──────────────────────────────────┘
                                │ SQLAlchemy
                    ┌───────────▼───────────┐
                    │ SQLite (desktop)      │
                    │ PostgreSQL (server)   │
                    └───────────────────────┘
```

---

## 3. Layering

Dependencies point inward; nothing in an inner ring imports an outer one.

| Layer | Contains | May import |
| --- | --- | --- |
| `api/` | Routers, request/response models, auth dependencies | services, domain, infra |
| `services/` | Business logic | domain, infra |
| `domain/` | ORM models, repositories | infra (database only) |
| `infrastructure/` | DB, logging, events, scheduler, metrics | config only |
| `core/` | Errors, middleware, startup validation | infra |

Routers stay thin: they validate, delegate and serialise. A router that grows
logic is a smell.

---

## 4. Execution engine

The heart of the system (`services/workflow/`), built in M4 and hardened in M5.

### Lifecycle

```
POST /api/workflows/{id}/executions
        │
        ▼
  admission → bounded priority queue  (429 when full)
        │
        ▼
  worker pool (EXECUTION_MAX_WORKERS)
        │
        ▼
  run_execution_v2
    ├── load graph snapshot
    ├── validate DAG (cycle detection)
    ├── topological wave scheduling
    │     ├── branch gating (condition nodes suppress unreachable subgraphs)
    │     ├── bounded loops
    │     └── per-node timeout · retry with backoff · error classification
    ├── persist node results (batched)
    ├── publish events → SSE broker → editor
    └── finalise + metrics
```

### Design decisions worth knowing

**Event-driven, not polling.** The M1 engine busy-waited at 100% CPU on blocked
graphs. Scheduling is now driven by completion events.

**Node results are serialised through a write lock.** Parallel writes
previously corrupted rows. The lock is process-wide, which serialises status
writes across concurrent runs — a known bottleneck, acceptable at current
scale.

**Retryability is a property of the error, not the attempt.** `NodeErrorCode`
classifies each failure; validation and permission errors fail fast while
network, timeout and provider errors back off and retry. A misconfigured node
does not burn three attempts.

**Only `None` means "no output".** `{}`, `0` and `False` are real values; an
early version discarded them.

**Back-pressure at every boundary.** The queue is bounded, SSE subscriber
queues are bounded (a slow client is dropped, never allowed to stall the
engine), log writes are batched, and node outputs are truncated before
persistence.

---

## 5. Node runtime

Every node executes through `RuntimeNodeExecutor`, which wraps the M1
`BaseNodeExecutor` contract rather than replacing it — all older executors keep
working.

The runtime provides declarative `NodeSchema` input/output specs (with
coercion and validation), metrics, stable error classification, and flag
gating (`requires_flag`) for dangerous nodes.

Schemas are served at `/api/system/node-schemas`, and the frontend node
components mirror them, so a node configured in the editor validates
server-side without translation.

**73 registered node types** across 22 canonical types plus aliases.

---

## 6. Security architecture (M5)

Detailed in `SECURITY.md`; the structural points:

- **`Principal`** is the single object the application reasons about. JWT, API
  key, anonymous and auth-disabled callers all resolve to one, so routers never
  branch on authentication mechanism.
- **Authorization is a dependency**, not a convention:
  `Depends(require_write)`. A route without one is visibly unprotected.
- **Roles are defined once** (`ROLE_PERMISSIONS`) and reused by both the M0
  enterprise API and M5 enforcement — there is no second, competing model.
- **Effective permissions are an intersection** of role grants and API-key
  scopes, so a key can only ever narrow authority.
- **The script sandbox is a separate OS process** with kernel-enforced limits
  and a PEP 578 audit hook, not an in-process interpreter restriction.

---

## 7. Observability

- **Structured logging** with `request_id` (per request) and `correlation_id`
  (per logical operation, spanning several requests), propagated through
  contextvars so no call site has to thread them manually. Credential-shaped
  strings are redacted before they reach a handler.
- **Metrics**: a dependency-free Prometheus registry
  (`infrastructure/observability/metrics.py`). Path labels use the **route
  template**, so `/api/workflows/{id}` is one series regardless of id, and
  unmatched paths collapse to `/<unmatched>` so a scanner cannot explode
  cardinality.
- **Error aggregation**: bounded in-process grouping by fingerprint, exposed at
  `/api/system/errors`. In-process only — lost on restart, not cross-replica.
- **Probes**: `/health/live` (cheap, no DB), `/health/ready` (DB, scheduler,
  workers, configuration; 503 when degraded).

---

## 8. Data model

```
Project ─┬─ Workflow ─┬─ Node ──┐
         │            ├─ Edge ──┤
         │            └─ WorkflowExecution ─┬─ NodeExecution
         │                                  └─ ExecutionLog
         └─ MediaAsset ── ProcessingJob

Conversation ── Message                 AIModelRegistry · TokenUsage
User ─┬─ ApiKey                         AuditEvent · Plugin
      └─ RefreshSession
```

All hot foreign keys are indexed, with composite indexes on the queue and
history access paths. Cascade rules are declared at both the ORM and database
level. Alembic is the source of truth for schema; `create_all()` exists only
for fresh local installs and tests, and a migration test now asserts the two
cannot drift.

---

## 9. Deliberate constraints

Choices that look like omissions but are intentional:

**A small dependency set.** Creator OS must install without a compiler
toolchain, so JWT (HS256), password hashing (PBKDF2) and Prometheus exposition
are implemented against the standard library rather than pulling in PyJWT,
bcrypt/argon2 and `prometheus_client`. Each is ~100–300 lines and fully tested.
The trade-off is real: these are maintained code rather than battle-tested
libraries. If the deployment story ever allows C extensions, argon2id would be
the better password hash.

**Backward compatibility is non-negotiable.** V1.0 endpoint shapes, the
`/health` payload, `enterprise_auth`'s API and the unprefixed `/api` routes are
all preserved. M5 added `/api/v1` alongside, never instead.

**Authentication defaults off.** A desktop user must not be forced to log into
their own machine. Production safety comes from startup validation refusing the
unsafe combination, not from an inconvenient default.

---

## 10. Known architectural limits

| Limit | Consequence |
| --- | --- |
| Single-process execution | In-memory queue; >1 replica risks double execution. Queued runs are lost on restart. |
| Process-wide engine write lock | Node status writes serialise across concurrent runs. |
| In-memory rate limiter | Per-process budgets; multiplies with process count. |
| In-memory SSE broker | A client sees only executions on the replica it is connected to. |
| Global RBAC | No per-workflow ownership or tenancy. |
| `resume_failed` re-traverses the graph | Completed non-idempotent nodes re-run. |
| No inbound webhook triggers | The `webhook` node is outbound only. |

The single-process constraint is the dominant one: durable queueing with
database-level claim/lease is the prerequisite for horizontal scaling, and is
the natural next milestone.
