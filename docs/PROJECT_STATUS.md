# Project Status

**Current phase:** Release Candidate
**Version:** 1.1.0-rc1
**Last updated:** 2026-07-27 (M7)

## Milestone progress

| # | Milestone | Status |
| --- | --- | --- |
| M0 | Repair & hygiene | ✅ Complete (merged) |
| M1 | Backend core hardening | ✅ Complete (merged) |
| M2 | API expansion & service completion | ✅ Complete (merged, PR #3) |
| M3 | Drag-and-drop Workflow Editor | ✅ Complete (merged, PR #4) |
| M4 | Execution engine & AI orchestration | ✅ Complete (merged, PR #5) |
| M5 | Production readiness & platform hardening | ✅ Complete (merged, PR #7) |
| M6 | Production validation, scalability & operational readiness | ✅ Complete (merged, PR #8) — 85%, see `M6_VALIDATION_REPORT.md` |
| M7 | Production deployment & Release Candidate | ✅ Complete (this branch) — 88%, see `M7_RELEASE_AUDIT.md` |
| M8 | Durable queue & horizontal scaling (Redis) | ⬜ Planned |
| M9 | Media pipeline UX & first-party providers | ⬜ Planned |

## Health — all figures measured, not estimated

| Metric | V1.0 (as found) | After M4 | Now (M5) |
| --- | --- | --- | --- |
| Backend build | ✅ | ✅ | ✅ |
| Frontend build | ❌ broken (TS1005) | ✅ | ✅ **warning-free** |
| Backend tests | 19 passed / 1 failed | 1085 passed | **1342 passed / 0 failed** |
| Backend coverage | 82% | not re-measured | **89%** (re-measured this milestone) |
| Frontend tests | none | 105 passed | **179 passed / 0 failed** |
| Frontend typecheck | ❌ broken | ✅ | ✅ |
| Alembic migrations | not exercised | present, never run in CI | **✅ upgrade/downgrade round-trip tested** |
| Authentication | none | none | **✅ implemented** |
| RBAC enforcement | none | defined but never called | **✅ enforced per endpoint** |
| Deployment assets | none | none | **✅ written, not yet executed** |
| CI | never run | never run | **still never run** (needs a maintainer) |

Backend 22,700 LOC · frontend 4,900 LOC (excluding dependencies).

## Recent work (M5)

Turned a functional application into a deployable platform. A full audit
(`M5_GAP_ANALYSIS.md`) preceded any code, and rated baseline production
readiness at ~35%: the application layer was strong, the platform layer was
close to absent.

**Security.** The platform had no notion of who was calling it — all ~80
endpoints were anonymous, and the RBAC model defined in M0 was never once
enforced. M5 added users, API keys and refresh sessions, PBKDF2 password
hashing, dependency-free HS256 JWTs with algorithm pinning, and permission
dependencies applied per endpoint. API-key scopes intersect the owner's role so
they can only narrow authority. Added CSRF, trusted hosts, HSTS and
credential-keyed rate limiting.

**Sandbox.** Python nodes now run in a separate OS process with kernel-enforced
CPU and memory limits, closing two defects M4 could not: an infinite loop
pinned a core for the life of the backend, and a large allocation OOM-killed
the whole service. A PEP 578 audit hook — not the import allowlist — is the
enforcement boundary, and post-escape containment is tested. It is documented
as defence in depth, **not** a security boundary.

**Database.** `audit_events` had been an ORM model since V1.0 with no migration
at all, so migration-only deployments started without the table. Fixed, and a
test now asserts every ORM table has a migration.

**Frontend.** The Workflows tab rendered placeholder text, so the entire M3/M4
editor was unreachable from the running app. Mounting it exposed that **20 of
the 22 node component files had been committed as zero-byte files** — invisible
because they were never bundled. All 20 are implemented and tested.

### M5 verification

| Check | Result |
| --- | --- |
| Backend tests | 1342 passed (1085 pre-existing + 257 new M5) |
| Backend coverage | 89% |
| Frontend tests | 179 passed (105 pre-existing + 74 new) |
| Frontend typecheck | `tsc --noEmit` clean |
| Frontend build | `vite build` clean, no warnings |
| Migrations | upgrade → downgrade → re-upgrade verified on SQLite |
| Sandbox containment | verified, including post-escape |
| Docker images | **not built** — no container runtime available |
| Multi-process deployment | **not tested** — known to be unsupported |

### M5 known limitations

- Single-process execution only. The queue is in-memory, lost on restart, and
  running >1 replica risks double execution. Rate limiting and SSE fan-out are
  likewise per-process.
- RBAC is global; no per-workflow ownership or tenancy.
- The JavaScript node is not sandboxed. The Python sandbox is defence in depth,
  not a security boundary.
- Audit coverage is partial (auth and user administration) and not
  tamper-evident.
- Deployment assets are written but have never been executed end to end.
- CI has still never run; activation needs a maintainer to move the workflow
  into `.github/workflows/`.
- No external security review or penetration test has been performed.

## Recent work (M7 — Release Candidate)

M7 added no features. It attempted, from a clean clone, to do exactly what the
documentation instructed, and fixed what did not work.

Two release-blocking configuration defects were found that way — neither
visible from reading the code, both requiring the software to actually be run.

**M7-F1 (critical).** `.env` was resolved relative to the working directory, so
the file every guide tells you to create at the repository root was silently
ignored when starting the server from `backend/`. The process did not fail: it
fell back to every default, coming up in `development` on SQLite with
**authentication off** and **Swagger exposed**, while the migrated PostgreSQL
database sat unused. The M5 startup gate could not catch it, because that gate
only engages when it believes it is in production — and `ENVIRONMENT` had
itself defaulted back to `development`.

**M7-F2 (high, present since M6).** The M6 custom settings sources discarded
the configuration pydantic-settings had already resolved, so
`Settings(_env_file=...)` silently ignored the file. Found when the M7-F1
regression tests failed against a correct fix.

### M7 verification — all executed, none inferred

| Check | Result |
| --- | --- |
| Backend tests (SQLite) | **1484 passed**, 8 skipped, 0 failed |
| Backend tests (PostgreSQL 16.2) | **1492 passed, 0 skipped**, 0 failed |
| Backend coverage | **89%** |
| Frontend tests | **179 passed** |
| Frontend typecheck / production build | clean · 343.85 kB (109.08 kB gzip) |
| Fresh clone → install → migrate → boot → execute → shutdown | ✅ verified |
| Production boot on PostgreSQL | ✅ `/docs` 404, unauth API 401, bad Host 400 |
| Bootstrap admin → JWT login → RBAC | ✅ verified |
| Secret redaction in logs | ✅ passwords appear **0 times** |
| Migrations upgrade / downgrade / round trip | ✅ **0 orphaned enum types** |
| Full downgrade to base → re-upgrade | ✅ 19 tables restored |
| Backup → destructive delete → restore | ✅ all rows recovered |
| Restart persistence | ✅ data intact |
| Example workflows | ✅ **4/4 executed** against a live backend |
| Docker image build / `compose up` | ❌ **unverified — no container runtime** |

**Closed M6-5:** the 8 PostgreSQL migration regression tests, which had never
executed in M5 or M6 for want of a server, now run and pass.

### M7 known limitations

- **Docker has never been executed** — third milestone running. No
  `docker`/`podman`/`nerdctl` binary, no socket, every registry unreachable.
  Mitigated by verifying every process the containers would run *outside* a
  container, plus 23 static asset-consistency tests. Not a substitute.
- Single-process execution, in-memory queue, per-process rate limiting and SSE
  — all unchanged from M5/M6.
- CI has still never run; activation needs a maintainer.
- No licence file is present.
- Log rotation is configured but a rollover was not triggered (needs 10 MB).
- Electron desktop shell not launched in this environment.

## Estimated overall completion

**88% Release Candidate readiness**, measured per deployment path:

| Dimension | Weight | Score | Basis |
| --- | --- | --- | --- |
| Source installation | 20% | 100% | fresh clone verified end to end |
| PostgreSQL deployment | 20% | 100% | full suite + production posture verified |
| Operations | 15% | 95% | backup/restore/restart/rollback verified |
| **Docker deployment** | 20% | **25%** | assets validated statically; **never run** |
| Documentation | 15% | 95% | rewritten against the running system |
| Examples | 10% | 100% | 4/4 executed |

Deliberately not higher: **one of the five documented deployment paths has never
been executed.** Anything above 95% would be an assertion rather than a
measurement. Verifying Docker on a machine with a container runtime is the
single highest-value action remaining.

## Previous work

See `CHANGELOG.md` for M0–M4.
