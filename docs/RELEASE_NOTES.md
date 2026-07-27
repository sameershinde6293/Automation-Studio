# Release Notes

## v1.1.0-rc1 — Release Candidate 1

**2026-07-27**

The first build of Creator OS whose deployment path has been executed rather
than described. M7 added no features. It attempted, from a clean clone, to do
what the documentation said — and fixed what did not work.

Two release-blocking configuration defects were found that way. Neither was
visible from reading the code; both required actually running it.

---

### Release-blocking fixes

#### `.env` at the repository root was silently ignored — **critical**

Settings resolved `.env` relative to the **current working directory**. Every
guide says to write `.env` at the repository root and start the server from
`backend/`. Those are different directories, so the file was never read — and
the process **did not fail**. It fell back to every default.

Reproduced against a real server with a fully populated production `.env`:

| Setting | `.env` said | Process used |
| --- | --- | --- |
| `ENVIRONMENT` | `production` | `development` |
| `AUTH_ENABLED` | `true` | **`false`** |
| `ENABLE_DOCS` | `false` | **`true`** |
| `DATABASE_URL` | PostgreSQL | SQLite |

`/docs` served publicly, `/api/workflows/` returning `500` from an empty SQLite
file, and **every API caller treated as a local administrator**. The M5 startup
gate could not catch it: it only refuses to boot when it believes it is in
production, and `ENVIRONMENT` had itself defaulted back to `development`.

Fixed with deterministic discovery — the repository root and `backend/` are
located from the module's own path, so the same `.env` is found regardless of
where the process starts. The working-directory file still wins, so **no
existing deployment changes behaviour**. `CREATOR_OS_ENV_FILE` overrides the
search.

> **Upgrading from an affected build:** settings that were being ignored will
> now take effect. Verify what loads before you restart —
> [UPGRADE_GUIDE.md](UPGRADE_GUIDE.md).

#### Custom settings sources discarded their configuration — **high** (present since M6)

The M6 list-friendly settings sources were constructed with only `settings_cls`,
so every other resolved argument — including the per-instance `_env_file`
override — was thrown away. `Settings(_env_file=...)` silently returned
defaults.

Found while writing the regression tests for the defect above: they failed
against a *correct* fix, because the argument never reached the source. The
running server was unaffected; testability and any alternate-config loader were
not.

---

### Verification

Everything below was executed. Nothing is inferred.

| | |
| --- | --- |
| Backend tests (SQLite) | **1487 passed**, 8 skipped, 0 failed |
| Backend tests (PostgreSQL 16.2) | **1446 passed, 0 skipped**, 0 failed |
| Backend coverage | **89%** |
| Frontend tests | **179 passed** |
| Typecheck / production build | clean · 343.85 kB (109.08 kB gzip) |
| Example workflows | **4/4 executed** against a live backend |
| Fresh clone → run | verified end to end |
| PostgreSQL production boot | verified |
| Migrations: upgrade / downgrade / round trip | verified, **0 orphaned enums** |
| Backup → destructive delete → restore | verified, all rows recovered |
| Restart persistence | verified |
| Graceful shutdown | verified |
| Secret redaction in logs | verified — passwords appear **0 times** |

**Closed: M6-5.** The 8 PostgreSQL migration regression tests had never run —
they skip without `TEST_POSTGRES_URL`, and no PostgreSQL was available in M5 or
M6. They now execute and pass against real PostgreSQL 16.2.

---

### Docker is still unverified

`docker build` and `docker compose up` have **never been executed**, for the
third consecutive milestone. No container runtime is available: no
`docker`/`podman`/`nerdctl` binary, no socket, every container registry
unreachable, and `podman` absent from the package sources.

Mitigated, not solved:

- every process the containers would run has been verified outside them — same
  PostgreSQL 16.2, same production settings, same Uvicorn command line, same
  probes, same migration and backup commands;
- **23 new static tests** validate the assets: compose topology, the `${VAR}`
  contract against `.env.production.example`, nginx upstream host and port
  against the real compose services, container probe paths against the live
  FastAPI route table, one-shot migration wiring, unprivileged user, and
  `.dockerignore` coverage.

Treat your first containerised deployment as a validation exercise.

---

### New in this release

**Example workflows** — four production-shaped workflows in `examples/`,
covering AI chaining, resilient HTTP with retries and failure branching,
scheduled batch processing, and a dependency-free smoke test.

**`scripts/verify_examples.py`** — imports, validates, runs, reads back and
exports every example against a live backend, asserting the round trip. It
earned its place immediately: the first run caught two examples whose
`{{ Start.topic }}` templates rendered **empty**. The correct form is
`{{ Start.variables.topic }}`. Four broken example files would otherwise have
shipped.

**Documentation** — `README`, `INSTALLATION_GUIDE`, `TROUBLESHOOTING` (new),
`FAQ` (new), `UPGRADE_GUIDE` (new) and `M7_RELEASE_AUDIT` (new) rewritten
against the running system. `INSTALLATION_GUIDE` was 19 lines that named no
prerequisites; `RELEASE_NOTES` still described 0.3.0-alpha.

**46 new tests** — 23 configuration regression, 23 Docker static validation.
The regression tests were checked against the pre-fix code: 6 fail without the
fix. They are guards, not tautologies.

---

### Known limitations

Unchanged from M6 unless noted.

| | |
| --- | --- |
| **Docker unverified** | Never executed. See above |
| Single process only | The execution queue, rate limiter and SSE broker are in-memory. Keep `WEB_CONCURRENCY=1` and one replica, or risk double execution |
| Queued runs lost on restart | Rows persist as `QUEUED` but are never re-claimed |
| Script sandbox is not a security boundary | Defence in depth only. The JavaScript node is unsandboxed. Both disabled by default |
| RBAC is global | Any `editor` can modify any workflow |
| Access tokens cannot be revoked early | 15-minute default TTL |
| CI has never run | Needs a maintainer to copy `ci/github-actions-ci.yml` into `.github/workflows/` |
| No licence file | All rights reserved by default |

Environment-dependent, not product defects:

- **TLS interception** breaks outbound HTTPS from workflow nodes (`httpx` uses
  the `certifi` bundle, which lacks a corporate proxy CA). Start with
  `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`. Proven environmental: the
  identical request returns `200` against the system trust store.
- **Log rotation rollover** not triggered — the handler is configured correctly,
  but exercising it needs 10 MB of output.
- **Electron desktop shell** not launched; the binary download is blocked here.
  Browser build, tests and typecheck all pass.

Full list: [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

---

### Readiness: 88%

| Dimension | Weight | Score |
| --- | --- | --- |
| Source installation | 20% | 100% |
| PostgreSQL deployment | 20% | 100% |
| Operations | 15% | 95% |
| **Docker deployment** | 20% | **25%** |
| Documentation | 15% | 95% |
| Examples | 10% | 100% |

Not higher, deliberately: one of five documented deployment paths has never been
run. Verifying Docker on any machine with a container runtime is the single
highest-value action remaining. Full accounting:
[M7_RELEASE_AUDIT.md](M7_RELEASE_AUDIT.md).

---

## Earlier milestones

### M6 — Production validation (2026-07-26)
Executed the M5 deployment path against real PostgreSQL for the first time.
Found six defects, fixed five: comma-separated list settings crashed the process
at import; `downgrade` orphaned PostgreSQL enum types; `.env.production.example`
had never been committed; the connection pool was sized 5× too small (measured:
capacity 80 serves 100 concurrent clients with zero errors, versus 16% errors at
capacity 15).

### M5 — Production hardening (2026-07-26)
Authentication, API keys, per-endpoint RBAC, CSRF, trusted hosts, HSTS. A
process-isolated script sandbox with kernel resource limits. The missing
`audit_events` migration. Found and fixed **20 of 22 node components committed
as zero-byte files** — invisible because the editor was never mounted.

### M4 — Execution engine (2026-07-26)
DAG execution with branch gating, loops, pause/resume/stop, replay,
resume-failed, priority queue, SSE streaming, AI orchestration with provider
fallback and cost tracking.

### M3 — Workflow editor
React Flow canvas, 22 node types, drag-to-connect, cycle prevention, clipboard,
undo/redo, autosave.

### M0–M2 — Foundation
Repository repair, backend hardening, API expansion, media pipeline, AI runtime.

Full history: [CHANGELOG.md](CHANGELOG.md).
