# Release Notes

## v1.1.1 — Security patch release

**2026-07-28**

**Recommended for all v1.1.0 deployments.** A post-GA independent audit found
one Critical and four High defects that the v1.1.0 certification missed, plus
one regression introduced by the first Critical fix and caught during
stabilization. Six security-relevant fixes in total. **No features were added
and no working code was refactored.**

Upgrading is a drop-in replacement: **no database migration, no configuration
change and no API change.** Two behaviours become stricter (see *Behaviour
changes* below) — review them before rolling out.

### Who should upgrade urgently

| If you… | Then… |
| --- | --- |
| Run workflows containing an **HTTP or Webhook node** with URLs a user can influence | **Upgrade now.** CVE-class SSRF: the guard could be walked past with a redirect (AUDIT-1), and the first fix additionally leaked `Authorization` across origins (AUDIT-1a) |
| Expose the API to any untrusted network with `AUTH_ENABLED=true` | **Upgrade now.** Login throttling could be bypassed entirely (AUDIT-2) and four endpoints answered anonymously (AUDIT-3) |
| Use the **Email node** with bcc | **Upgrade now.** bcc addresses were both disclosed to all recipients and never delivered (AUDIT-4) |
| Configure `OPENAI_API_KEY` or `OPENAI_BASE_URL`/`OLLAMA_BASE_URL` in `.env` | **Upgrade.** These settings had no effect; AI calls silently fell back to another provider (AUDIT-5) |
| Run local desktop mode with no auth and no HTTP nodes | Low urgency; upgrade at convenience |

### Security fixes

**AUDIT-1 · Critical · SSRF guard bypassed by HTTP redirect**
`validate_outbound_url()` correctly blocks loopback, private, link-local and
cloud-metadata addresses — but was applied only to the *initial* URL, while the
HTTP client was configured with `follow_redirects=True`. Any attacker-influenced
public host could answer `302 Location: http://169.254.169.254/...` and the
request was made. Redirects are now followed manually with **every hop
re-validated**. Affected both HTTP node implementations; both are fixed.

**AUDIT-1a · High · Credentials leaked across origins by the redirect fix**
Found while re-reviewing AUDIT-1 during stabilization, before release. httpx
strips `Authorization`/`Cookie` when a redirect leaves the origin; the first
version of the manual redirect loop forwarded every header, handing the
caller's bearer token to whatever host a redirect named. Credential headers are
now stripped on cross-origin hops and preserved on same-origin hops and
HTTP→HTTPS upgrades, matching httpx exactly.

**AUDIT-2 · High · Login rate limiter evaded by rotating a header**
The limiter keyed on the presented credential — which on a login endpoint is
precisely what the attacker is guessing — so every attempt with a different
junk `Authorization` header got its own fresh bucket. Measured: 15/15 attempts
admitted against a 3-per-minute budget. Credential endpoints now bucket by
network address.

**AUDIT-3 · High · Deployment detail readable without a credential**
With `AUTH_ENABLED=true`, `/api/system/info`, `/metrics`, `/events` and
`/scheduler/jobs` all answered `200` to anonymous callers, disclosing the OS
build, Python patch version, **which risky executors are enabled**, the PID,
memory use and live workflow/node names. All four now require `read`. They had
been explicitly allowlisted in the route-coverage test, so the check designed
to catch unprotected endpoints was told to skip them.

**AUDIT-4 · High · Email node leaked bcc recipients and never delivered them**
Two defects with opposite effects: bcc addresses were written into the visible
`To:` header (disclosed to every recipient), and because the SMTP envelope was
derived from headers — where a `Bcc:` header is correctly never written — bcc
was simultaneously **never delivered**, while the node returned `sent: true`.
Verified against a real SMTP server before and after.

**AUDIT-5 · High · AI providers ignored their own configuration**
`OpenAIProvider` read `os.environ` directly, but settings are loaded from
`.env` by pydantic-settings, which does not populate the process environment.
A key configured the documented way was invisible, so every call raised
`OPENAI_API_KEY is not set` and the orchestrator **silently fell back** to
Ollama or the mock provider — answers were quietly wrong rather than failing
loudly. `OPENAI_BASE_URL` and `OLLAMA_BASE_URL` were hardcoded and had no
effect at all.

### Behaviour changes

Two changes are deliberately stricter and may affect existing integrations:

1. **`/api/system/{info,metrics,events,scheduler/jobs}` now require the `read`
   permission** when `AUTH_ENABLED=true`. Unauthenticated monitoring scrapes of
   these paths will start returning `401`. `/api/system/node-types` and
   `/node-schemas` remain public for the editor palette, and `/health`,
   `/health/live`, `/health/ready` and `/metrics` are unchanged — use those for
   liveness and Prometheus.
2. **`Authorization`/`Cookie` headers set on an HTTP node are no longer sent
   after a cross-origin redirect.** This matches every mainstream HTTP client.
   A workflow that depended on the old leak-through behaviour must target the
   final URL directly.

### Verification

| Check | Result |
| --- | --- |
| Backend, SQLite | **1594 passed**, 10 skipped, 0 failed |
| Backend, PostgreSQL 16.2 | **1602 passed**, 2 skipped, 0 failed |
| Frontend (Vitest, 13 files) | **179 passed** |
| Frontend typecheck | clean |
| Production build | clean — 343.85 kB (109.08 kB gzip), 1735 modules |
| Security regression tests | **18 passed** (`backend/tests/audit/`) |
| Audit tests vs. pre-fix v1.1.0 tree | **13 of 18 fail**, as required |

### Upgrading

```bash
git fetch --tags && git checkout v1.1.1
cd backend && ./.venv/bin/pip install -r requirements.txt
cd ../frontend && ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm install && npm run build
```

No `alembic upgrade` is required — the schema is unchanged from v1.1.0.
Rollback is `scripts/rollback.sh v1.1.0`, with the caveat that doing so
reinstates all six defects above.

---

## v1.1.0 — General Availability

**2026-07-28**

The 1.1 line reaches GA. M10 added **no features**: it audited the whole
repository, re-ran every suite from scratch, re-executed the production
deployment path against real PostgreSQL 16.2, and corrected every claim that
earlier milestones had overstated.

### What changed in this release

Only release-certification changes. The version moves `1.1.0-rc3` → `1.1.0`
across the backend, frontend, lockfile, health payload and all documentation
headers — five doc headers were still stamped `v1.1.0-rc1`, a drift that had
survived three milestones because the consistency test only covered the README
and `PROJECT_STATUS.md`. That test now covers the documentation headers too.

### Defects fixed

- **`SSL_CERT_FILE` was applied to the wrong process** (M10-F1). The documented
  TLS-interception workaround was exported for `verify_examples.py`, but the
  HTTP node runs *inside the backend*, so the variable never reached the code
  making the request. `scripts/ci-local.sh` and the CI `examples` job now export
  it for the backend; 4/4 examples pass.
- **Three overstated claims corrected** (M10-F2/F3/F4): stale test totals in
  `TEST_COVERAGE.md`, two different milestones both numbered M10 in
  `PROJECT_STATUS.md`, and the assertion that CI was "activated in M8" — it
  never was, and still has not run.

### Verified in this release

| | |
| --- | --- |
| Backend tests | **1576 passed / 10 skipped** (SQLite) · **1584 passed / 2 skipped** (PostgreSQL 16.2) · 0 failed |
| Backend coverage | **89%** (7782 statements) |
| Frontend | **179 passed**, typecheck clean, build 343.85 kB (109.08 kB gzip) |
| Production boot | PostgreSQL 16.2, `/docs` 404, unauth 401, bad Host 400, startup 41 ms |
| Disaster recovery | dump → `DROP SCHEMA CASCADE` → restore → 20 tables, all rows, app authenticates |
| Failure injection | DB loss → 503 ready / 200 live, recovery ~1 s without restart; SIGKILL leaves no orphans |
| Migrations | PostgreSQL upgrade → downgrade to base → re-upgrade, 0 orphaned enum types |
| Examples | **4/4** on an authenticated production backend |
| Observability | 14 metric families live; secrets appear 0 times in logs |

### Known limitations — unchanged and stated plainly

**Docker has never been executed.** Five consecutive milestones, no container
runtime available and no reachable registry. Static validation only (44 checks
+ 53 tests). **CI has never run** — pushing `.github/workflows/` is rejected for
the automation account. No 24-hour soak, no multi-replica deployment, no TLS
termination executed, and **no LICENSE file**. Production readiness is **94%**,
deliberately not higher. See `docs/M10_RELEASE_CERTIFICATION.md`.

---

## v1.1.0-rc3 — Release Candidate 3

**2026-07-28**

The first build validated against a **real PostgreSQL server**. M9 added no
features: it deployed Creator OS to a production-shaped staging environment,
measured it, attacked it, and fixed the four defects that came out.

Earlier milestones reported that no PostgreSQL was available in this
environment. That was true for `apt` but incomplete — PyPI is reachable, and
the `pgserver` wheel ships a complete PostgreSQL 16.2 distribution. Running
against the same major version the compose file pins turned eight
long-skipped migration tests into passing tests and exposed defects that
SQLite could not.

### The one that matters

**`scripts/backup.sh` reported success while backing up nothing.** Run against
the PostgreSQL staging deployment it exited `0` having written no database
dump at all — `pg_dump` was absent, the branch printed a warning, and the
script carried on. A nightly cron would have reported success indefinitely and
the truth would have surfaced during a restore.

`restore.sh` had the matching defect: it piped into `psql "${DATABASE_URL:-}"`,
which is empty unless that variable happens to be exported, and ran without
`ON_ERROR_STOP`, so a half-applied dump still exited `0`.

Both now fail loudly, target the database named in `DATABASE_URL`, verify the
archive, checksum every artefact and honour `MEDIA_ROOT`. Proven by a full
disaster-recovery drill that ends with the application serving traffic from
the restored database.

### Also fixed

- **Database pool metrics** (`creator_os_db_pool_*`) — pool capacity was
  documented as the concurrency limit and measured in M6, but never exported,
  so saturation was invisible at run time.
- **`auth.account.locked` audit event** — lockouts were logged but never
  written to the audit trail.
- **Version consistency** — README advertised rc2, the code shipped rc1.
  Everything is rc3, enforced by a test.

### Measured

| | |
| --- | --- |
| Long run | 48 min, 408 executions, **0 failures**, RSS 91→100 MB, CPU 1.0% of a core |
| API | `/health` p95 2.9 ms · `/api/workflows/` p95 8.4 ms · login 210 ms (PBKDF2, by design) |
| Workflow | p50 57 ms, p95 68 ms; 20 concurrent runs all completed |
| Startup / shutdown | 1.1 s / 188 ms |
| Failure recovery | DB loss → 503 ready, 200 live, recovers in **1 s** without a restart |
| Tests | 1562 (SQLite) · 1570 (PostgreSQL) · 179 frontend · 4/4 examples |

### Known limitations

Docker has still never been executed here — no runtime, no reachable registry.
48 minutes is not a 24-hour soak, and multi-replica operation is untested.
Readiness is **94%**, deliberately not higher. See
`M9_VALIDATION_REPORT.md` §9 and §11.

---

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
| Backend tests (SQLite) | **1484 passed**, 8 skipped, 0 failed |
| Backend tests (PostgreSQL 16.2) | **1492 passed, 0 skipped**, 0 failed |
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
- **One flaky pre-existing test** (`test_path_label_uses_the_route_template`,
  M5, untouched by M7): seen once in five full PostgreSQL runs when `12345`
  collided with a float timing value in the metrics body. Verified not a
  labelling defect — no `path=` label ever carried a raw id, 5/5 in isolation,
  and a full re-run passed 1492. Tracked as M7-7.

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
