# Test Coverage

## M4 totals (2026-07-26)

| Suite | Tests | Status |
| --- | --- | --- |
| Backend — pre-existing (M0–M3) | 825 | ✅ all passing, none modified |
| Backend — new in M4 (`tests/m4/`) | 260 | ✅ all passing |
| **Backend total** | **1085** | ✅ |
| Frontend — pre-existing (M3) | 11 | ✅ now runnable for the first time |
| Frontend — new in M4 | 94 | ✅ all passing |
| **Frontend total** | **105** | ✅ |

### M4 backend suites

| File | Focus |
| --- | --- |
| `test_engine_execution_m4.py` | Graph execution, branch gating, loops, retries, pause/resume/stop, cancellation, streaming, guard rails |
| `test_queue_scheduler_m4.py` | Priority queue, admission control, worker pool, control handles, runtime primitives |
| `test_history_orchestration_m4.py` | History search/filter/stats, replay, resume-failed, lineage, circuit breaker, cost model, traces, provider fallback |
| `test_execution_api_m4.py` | Execution control/history/streaming APIs, node catalog and schemas, AI cost/trace endpoints |
| `test_node_library_m4.py` | All 23 node types: control, network SSRF, script gating, file/folder confinement, media provider errors |

### M4 frontend suites

| File | Focus |
| --- | --- |
| `executionStore.test.ts` | Event application, id mapping, hydration, controls |
| `executionControls.test.tsx` | Button availability per status, dispatch |
| `logViewer.test.tsx` | Log rendering, filtering, search, progress bar |
| `historyPanel.test.tsx` | Listing, filtering, replay, resume-failed |
| `executionApi.test.ts` | Request shapes, error envelope, graph adapter, SSE + polling |

### Caveats

- Backend line coverage was **not re-measured** in M4. The 94% figure in
  `PROJECT_STATUS.md` is the M2 measurement and should be treated as stale.
- Tests that would require real external services (SMTP delivery, live FFmpeg
  transcode, real AI providers) are exercised through their guarded paths —
  disabled flags, dry-run behaviour, provider-missing errors — not by
  contacting anything external.
- No end-to-end browser test drives the editor against a live backend.


---

## Historical


## Test Files Written
- `workflowStore.test.ts` — node CRUD, undo/redo, dirty state, serialization
- `baseNode.test.tsx` — rendering, config editing
- `canvas.test.tsx` — React Flow rendering
- `clipboard.test.ts` — copy/paste/duplicate
- `importExport.test.ts` — JSON import/export roundtrip

## Coverage Areas
- WorkflowStore (core state + persistence)
- BaseNode configuration & validation
- Canvas rendering & interaction
- Clipboard operations
- Import/Export & serialization

All tests are written in Vitest + React Testing Library and are ready to run once the environment allows `npm install`.