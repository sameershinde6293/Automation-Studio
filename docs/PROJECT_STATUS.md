# Project Status

**Current phase:** Version 1.1 development
**Version:** 1.1.0 (in progress)
**Last updated:** 2026-07-26 (M6)

## Milestone progress

| # | Milestone | Status |
| --- | --- | --- |
| M0 | Repair & hygiene | ✅ Complete (merged) |
| M1 | Backend core hardening | ✅ Complete (merged) |
| M2 | API expansion & service completion | ✅ Complete (merged, PR #3) |
| M3 | Drag-and-drop Workflow Editor | ✅ Complete (merged, PR #4) |
| M4 | Execution engine & AI orchestration | ✅ Complete (merged, PR #5) |
| M5 | Production readiness & platform hardening | ✅ Complete (merged, PR #7) |
| M6 | Production validation, scalability & operational readiness | ✅ Complete (this branch) — 85%, see `M6_VALIDATION_REPORT.md` |
| M7 | Durable queue & horizontal scaling (Redis) | ⬜ Planned |
| M7 | Media pipeline UX & first-party providers | ⬜ Planned |

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

## Estimated overall completion

**≈ 88%** toward a polished commercial-grade Creator OS, and **≈ 72%** toward
genuine production readiness for a multi-user deployment.

The core loop — design, run, watch, control, inspect, replay — is complete and
tested, and the platform now has identity, enforcement, observability and
deployment assets. What separates it from production-ready is operational
proof rather than missing features: the containers have not been run, CI has
not executed, and the single-process constraint must be lifted before the
platform can scale or survive a restart without losing queued work.

## Previous work

See `CHANGELOG.md` for M0–M4.
