# Developer Guide

## Architecture

- **Frontend:** React + Vite + Electron (TypeScript)
- **Backend:** FastAPI + SQLAlchemy + Alembic (Python)
- **Database:** SQLite (local first)

### Layering

The backend follows a domain-oriented layering; dependencies point inward and
never the other way.

```
app/
├── api/routers/        HTTP surface only — validation, status codes, no logic
│   ├── workflow_router     workflows, nodes, edges, graph, validate
│   ├── execution_router    control, history, logs, timeline, SSE   (M4)
│   ├── ai_router           models, conversations, chat, cost, traces
│   ├── media_router        assets, processing, ffmpeg
│   └── system_router       info, metrics, node-types, node-schemas
├── services/           business logic
│   ├── workflow/
│   │   ├── graph.py        pure algorithms (no ORM, no IO)
│   │   ├── engine.py       scheduler, retries, branching, loops, control
│   │   ├── runtime.py      NodeContext, schemas, metrics, error taxonomy
│   │   ├── queue.py        priority queue + worker pool
│   │   ├── control.py      pause / resume / stop handles
│   │   ├── streaming.py    event fan-out + batched log writer
│   │   ├── history.py      search, replay, resume-failed, stats
│   │   ├── executors.py    M1 executors + the registry
│   │   └── nodes/          the 23-type node library                (M4)
│   ├── ai/                 orchestrator, providers, fallback, cost
│   └── media/              storage, ffmpeg, pipeline
├── domain/             models + repositories (persistence contracts)
└── infrastructure/     config, database, logging, events, scheduler
```

**Rule of thumb:** `graph.py` must stay free of ORM and IO imports so it remains
cheap to unit-test; `engine.py` owns all persistence for a run; routers own no
business logic.

### Execution architecture

See [`EXECUTION_ENGINE.md`](EXECUTION_ENGINE.md) for the full picture. In short:

```
API → ExecutionQueue (priority, bounded) → WorkerPool → run_execution_v2
                                                              │
                        ┌─────────────────────────────────────┼──────────────┐
                        ▼                     ▼               ▼              ▼
                 graph validation      node scheduling   ControlHandle  ExecutionBroker
                                       (semaphore)       (pause/stop)   (SSE + logs)
```

### Frontend structure

```
src/
├── api/executionApi.ts     typed client + SSE stream (polling fallback)
├── stores/
│   ├── workflowStore.ts    canvas graph, undo/redo, persistence
│   ├── executionStore.ts   live execution state driven by SSE      (M4)
│   └── graphAdapter.ts     editor ⇄ backend graph translation      (M4)
├── components/
│   ├── WorkflowCanvas.tsx  React Flow canvas
│   ├── ExecutionPanel.tsx  nodes / logs / history tabs
│   ├── execution/          controls, progress, log viewer, history (M4)
│   └── nodes/              22 node components
└── test/setup.ts           jsdom stubs + MockEventSource
```

**Editor ids are UUID strings; backend ids are integers.** `graphAdapter.ts` is
the single place that conversion happens — do not inline it elsewhere.

## Development Workflow

1. Start the Python backend:
   ```bash
   cd backend
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   alembic upgrade head
   uvicorn app.main:app --reload
   ```
2. Start the frontend (Electron/Vite):
   ```bash
   cd frontend
   npm install
   npm run electron:dev      # or `npm run dev` for browser-only
   ```

> In restricted networks Electron's postinstall binary download may fail TLS
> verification. `npm install --ignore-scripts` installs everything needed for
> `npm test`, `npm run typecheck` and `npm run build`.

## Testing

```bash
# Backend — 1085 tests
cd backend && .venv/bin/python -m pytest            # full suite
.venv/bin/python -m pytest tests/m4                 # M4 only
.venv/bin/python -m pytest --ignore=tests/m4        # pre-M4 regression check

# Frontend — 105 tests
cd frontend && npm test
npm run typecheck
npm run build
```

### Writing a node

```python
from app.services.workflow.runtime import (
    FieldSpec, NodeSchema, RuntimeNodeExecutor,
)

class MyNode(RuntimeNodeExecutor):
    label = "My Node"
    category = "custom"
    aliases = ("my_node",)                 # snake_case alias
    requires_flag = None                   # e.g. "ALLOW_X" to gate it off
    schema = NodeSchema(
        inputs=[FieldSpec("url", "string", required=True)],
        outputs=[FieldSpec("body", "string")],
    )

    async def run(self, node, context, config):
        # `config` is already validated and coerced against `schema`.
        return {"body": await fetch(config["url"])}
```

Register it in `services/workflow/nodes/__init__.py::NODE_LIBRARY`. Add tests to
`backend/tests/m4/test_node_library_m4.py`.

Guidelines:
- Raise `ValidationError` for bad config (non-retryable) and
  `NodeExecutionError(code=...)` for runtime failures.
- Never contact the network or filesystem outside `MEDIA_ROOT` without going
  through `validate_outbound_url` / `resolve_media_path`.
- If a capability is unavailable, **fail with a clear error** — do not return
  fabricated output.

## Contribution Standards

- Write tests for all new services and nodes.
- `pytest`, `npm test`, `npm run typecheck` and `npm run build` must pass.
- Do not modify existing tests to accommodate new behaviour; if a pre-existing
  test fails, either the change is wrong or the test encoded a real bug — say
  which in the commit message.
- Extend the existing architecture rather than replacing working code.
- Never overstate completion in docs; record known limitations explicitly.
