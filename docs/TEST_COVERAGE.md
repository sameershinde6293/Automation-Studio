# Test Coverage

## Current totals (2026-07-28, M10 — v1.1.0)

All figures below were measured in the M10 certification run on this
environment. Nothing is carried forward from an earlier milestone.

| Suite | Tests | Status |
| --- | --- | --- |
| Backend — SQLite (default) | **1576 passed**, 10 skipped, 0 failed | ✅ |
| Backend — PostgreSQL 16.2 | **1584 passed**, 2 skipped, 0 failed | ✅ |
| Backend line coverage | **89%** (7782 statements, 866 uncovered) | ✅ measured, not estimated |
| Frontend (Vitest, 13 files) | **179 passed** | ✅ |
| Frontend typecheck (`tsc --noEmit`) | clean | ✅ |
| Frontend production build | clean, 343.85 kB (109.08 kB gzip), 1735 modules | ✅ |
| Example workflows executed end to end | **4/4** against an authenticated production backend | ✅ |
| E2E smoke (`e2e_execution_smoke.py`, `e2e_control_smoke.py`) | both pass | ✅ |

The 10 skips under SQLite are the PostgreSQL-gated tests, which key off
`TEST_POSTGRES_URL`. With a real PostgreSQL 16.2 server (supplied here by the
`pgserver` wheel) 8 of them execute, leaving 2 skips that are gated on other
optional tooling.

### New in M7 (46 tests)

| File | Focus |
| --- | --- |
| `tests/m7/test_env_discovery_m7.py` | `.env` search path, CWD independence, precedence order, `CREATOR_OS_ENV_FILE` override, and end-to-end loading against temporary trees (M7-F1) |
| `tests/m7/test_settings_sources_m7.py` | Per-instance `_env_file` reaching the custom sources, M6 CSV/JSON list parsing preserved, `init > env > .env` precedence (M7-F2) |
| `tests/m7/test_docker_assets_m7.py` | Compose topology, `${VAR}` contract vs `.env.production.example`, nginx upstream host/port vs real services, probe paths vs the live FastAPI route table, one-shot migration wiring, image hardening |

**These are verified guards, not tautologies.** Run against the pre-fix code, 6
of the configuration tests fail and the M7-F1 suite cannot even import. They
were confirmed to detect the defects they describe.

### How to run everything

```bash
./scripts/ci-local.sh                              # all of the below

cd backend && ./.venv/bin/python -m pytest -q      # 1576 passed, 10 skipped
cd frontend && npm test && npm run typecheck       # 179
python scripts/verify_examples.py                  # 4/4 examples

# include the PostgreSQL-gated tests
TEST_POSTGRES_URL=postgresql+psycopg://user:pass@localhost:5432/scratch \
  ./.venv/bin/python -m pytest -q
```

### Caveats

- No end-to-end browser test drives the editor against a live backend; frontend
  coverage is component and store level.
- Tests requiring real external services (SMTP delivery, live FFmpeg transcode,
  real AI providers) are exercised through their guarded paths — disabled flags,
  dry-run behaviour, provider-missing errors — not by contacting anything.
- **Docker is not covered by any executing test.** The 23 Docker tests are
  static consistency checks over the asset files; no image is built and no
  container is started. See `M7_RELEASE_AUDIT.md` §6.
- Log rotation rollover is not exercised (needs 10 MB of output).

---

## Historical

### M4 totals (2026-07-26)

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

All frontend tests are written in Vitest + React Testing Library. They are
executed on every milestone — most recently in M10: **179 passed, 13 files**.
(The earlier wording "ready to run once the environment allows `npm install`"
was stale from before the suite was first executed.)