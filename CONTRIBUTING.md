# Contributing to Creator OS

Thanks for working on Creator OS. This document covers how to get set up, what
the code is expected to look like, and what must be true before a change lands.

---

## Getting set up

### Backend (Python 3.11+)

```bash
cd backend
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

./.venv/bin/alembic upgrade head          # create/update the schema
./.venv/bin/uvicorn app.main:app --reload
```

> Run uvicorn against `app.main:app`, never `main:app`. `backend/main.py` is a
> V1.0 stub retained for compatibility and serves an app with no routers.

### Frontend (Node 22+)

```bash
cd frontend
ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm ci
npm run dev
```

The Electron binary download fails behind TLS-inspecting proxies. Skipping it
is fine for the browser app, tests, typecheck and build; only launching the
desktop shell needs it.

### Everything at once

```bash
./scripts/ci-local.sh
```

This runs exactly what CI would: backend tests with coverage, the migration
round-trip, frontend typecheck, build and tests.

---

## Before you open a pull request

All of these must pass:

```bash
# Backend
cd backend
./.venv/bin/python -m pytest                 # 1342 tests, all green
./.venv/bin/ruff check app                   # linting

# Frontend
cd frontend
npm test                                     # 179 tests
npm run typecheck                            # tsc --noEmit, no errors
npx tsc && npx vite build                    # must build without warnings
```

A red test suite is never "fixed later".

---

## Engineering rules

These are the conventions the codebase is held to. They exist because
violating them has caused real defects in this project.

### Never overstate completion
If something is partially done, say so — in the PR, in `KNOWN_ISSUES.md`, and
in the docstring. `SECURITY.md` documents the sandbox's *weaknesses* as
carefully as its strengths, and there is a test class
(`TestDocumentedLimitations`) whose job is to fail if those claims drift. Hold
new work to the same standard.

### Extend, don't replace
Working implementations stay. M4's `run_execution` was preserved verbatim when
`run_execution_v2` was added; M5's process sandbox kept the in-process path as
a fallback. If you believe something must be replaced, justify it in the PR and
keep the old path until the new one is proven.

### Preserve backward compatibility
V1.0 endpoint shapes, the `/health` payload, `enterprise_auth`'s public API and
the unprefixed `/api` routes are contracts. Add alongside (`/api/v1`), never
instead. A change that breaks an existing client needs an explicit migration
path.

### Safe defaults
Anything dangerous ships disabled. Script executors, the shell executor and
private-network HTTP are all `false` by default, and startup validation refuses
unsafe production combinations. A new capability that can execute code, reach
the network or touch the filesystem needs a flag and an entry in
`app/core/startup.py`.

### Tests before claims
Write the test that would have caught the bug. When a defect is found, the
first commit should contain a failing test. Several M5 tests exist specifically
as regression guards:
- `tests/m5/test_migrations_m5.py` asserts every ORM table has a migration
  (`audit_events` shipped without one for three milestones).
- `frontend/src/__tests__/nodeRegistry.test.tsx` renders all 22 node types
  (20 were committed as empty files and nothing noticed).

### Comments explain *why*
Assume the reader can see what the code does. Document the reasoning, the
trade-off, or the bug being avoided:

```python
# Limits are applied at the top of the child rather than via preexec_fn.
# preexec_fn runs between fork and exec in a process that has other threads,
# where only async-signal-safe calls are legal; taking a lock held by another
# thread at fork time would deadlock the child.
```

Do not leave `TODO`/`FIXME` in merged code. Either fix it, or file it in
`KNOWN_ISSUES.md` where it will be seen.

---

## Code style

### Python
- `from __future__ import annotations` at the top of every module.
- Type-hint public functions; use the typed error hierarchy in
  `app/core/errors.py` rather than bare `Exception`.
- Routers stay thin — validate, delegate, serialise. Logic belongs in
  `services/`.
- Never catch and silently `pass`. Log it, or let it propagate.
- Never log a credential. The logging filter redacts common patterns, but do
  not rely on it.

### TypeScript
- `import type { … }` for type-only imports — mixing them into value imports
  makes the bundler warn about exports that do not exist at runtime.
- Components are function components with explicit prop types.
- Clean up in `useEffect`: abort in-flight requests, clear timers, and never
  set state after unmount.

---

## Architecture boundaries

Dependencies point inward. `api/` → `services/` → `domain/` →
`infrastructure/`. An inner layer importing an outer one is a bug.

New workflow nodes derive from `RuntimeNodeExecutor` with a declarative
`NodeSchema`, and dangerous ones set `requires_flag`. If you add a node, add a
matching frontend component whose config fields mirror the schema.

---

## Database changes

1. Change the model in `app/domain/models/`.
2. Generate a migration:
   `./.venv/bin/alembic revision --autogenerate -m "describe the change"`.
3. **Read the generated file.** Autogenerate misses index and constraint
   changes and sometimes produces destructive operations.
4. Verify it round-trips:
   ```bash
   ./.venv/bin/python -m pytest tests/m5/test_migrations_m5.py
   ```
5. Keep a single head. Two heads cannot be applied linearly.

Migrations must be safe on a populated database: add nullable or
server-defaulted columns rather than rewriting rows.

---

## Commits and pull requests

- One logical change per commit; the subject says what and the body says why.
- Reference the milestone where relevant (`feat(M5): …`).
- A PR should state what was verified, with numbers, and what was *not*.
- List remaining limitations honestly. A PR that says "partially done, here is
  what is missing" is far more useful than one that implies completeness.

---

## Reporting security issues

Do not open a public issue for an exploitable defect. See `docs/SECURITY.md`
§9.
