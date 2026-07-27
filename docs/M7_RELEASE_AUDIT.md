# M7 — Release Audit (Release Candidate 1)

Creator OS v1.1.0-rc1 · audit executed 2026-07-27

M7 is not a feature milestone. Its question is narrow and testable:

> **Can someone clone this repository and successfully run it, with no prior
> knowledge of the project?**

Everything below was executed, not reviewed. Where something could not be
executed in this environment, it is recorded as **unverified** rather than
assumed to work.

---

## 1. Verdict

**Yes — for the source and PostgreSQL paths, with two release-blocking defects
fixed along the way.** The Docker path remains unverified for the third
consecutive milestone because no container runtime exists here.

| Deployment path | Status | Evidence |
| --- | --- | --- |
| Fresh clone → SQLite → dev server | ✅ verified | §3 |
| Fresh clone → PostgreSQL 16.2 → production server | ✅ verified | §4 |
| Migrations: upgrade / downgrade / round trip on PostgreSQL | ✅ verified | §5 |
| Backup / restore / restart persistence | ✅ verified | §5 |
| Docker image build and `docker compose up` | ❌ **unverified** | §6 |

**Release Candidate readiness: 88%.** The deduction is almost entirely the
Docker layer (§6) plus the single-process limits carried forward from M5/M6.

---

## 2. Defects found

Two release-blocking configuration defects were found by executing the
documented procedure rather than reading it. Both are fixed in this milestone,
both have regression tests that fail against the pre-fix code.

### M7-F1 — `.env` at the repository root was silently ignored (**critical**)

`Settings.model_config` used `env_file=".env"`, which pydantic-settings
resolves **relative to the process working directory**. Every guide in the
repository says to write `.env` at the repository root:

```bash
cp .env.production.example .env      # -> <repo>/.env
```

and then to start the server from `backend/`:

```bash
cd backend && uvicorn app.main:app   # CWD = <repo>/backend
```

Those are different directories. The file was never read, and **the process did
not fail** — it fell back to every default.

Reproduced against a real server (a fully populated production `.env` at the
repo root, PostgreSQL configured, auth enabled, docs disabled):

| Setting | `.env` said | Process used | Consequence |
| --- | --- | --- | --- |
| `ENVIRONMENT` | `production` | `development` | production gate never engaged |
| `AUTH_ENABLED` | `true` | `false` | **every API caller treated as a local admin** |
| `ENABLE_DOCS` | `false` | `true` | **Swagger served publicly** |
| `DATABASE_URL` | PostgreSQL | SQLite | wrote a stray `backend/creator_os.db` |

Observed HTTP behaviour before the fix: `/docs` → `200`, `/api/workflows/` →
`500` (`no such table: workflows`, because it was querying an empty SQLite file
rather than the migrated PostgreSQL database).

The M5 startup-validation gate could not catch this: it only refuses to boot
when it believes it is in production, and `ENVIRONMENT` had itself defaulted
back to `development`. A silent fallback to unauthenticated defaults is the
worst available failure mode for the one file that carries the security
posture.

**Fix.** Deterministic discovery instead of a working-directory guess. The
repository root and `backend/` are located from the module's own path, so the
same `.env` is found regardless of where the process starts. The
working-directory file is still read and still wins, so no existing deployment
changes behaviour — this only adds locations that previously resolved to
nothing. `CREATOR_OS_ENV_FILE` overrides the search for deployments that keep
secrets outside the tree.

After the fix, same scenario, started from `backend/`:

```
ENVIRONMENT  = production
AUTH_ENABLED = True
ENABLE_DOCS  = False
DATABASE_URL = postgresql+psycopg://…/creator
```

### M7-F2 — custom settings sources discarded their configuration (**high**, pre-existing since M6)

Found while writing the M7-F1 regression tests: the tests failed against a
*correct* fix, because the constructor argument never reached the source.

M6 replaced the two standard settings sources with list-friendly subclasses
(to make `CORS_ORIGINS=a,b` parse — M6-F1) and constructed them with only
`settings_cls`:

```python
_ListFriendlyEnvSource(settings_cls)
_ListFriendlyDotEnvSource(settings_cls)
```

Every other constructor argument therefore fell back to its default,
**discarding the configuration pydantic-settings had already resolved** —
including the per-instance `_env_file` override. So
`Settings(_env_file="/path/to/other.env")` silently ignored the file and
returned defaults.

Invisible in normal operation, because the module-level singleton passes no
overrides. It made the class untestable against a temporary `.env` and would
break any caller loading an alternate configuration.

**Fix.** The replacements now inherit the resolved attributes from the sources
they replace. The `.env` candidate list is re-evaluated at construction time
rather than reused from `model_config` — `model_config` is built once at class
creation, which would freeze the working directory as it was at *import* time,
whereas the pre-M7 relative `".env"` was resolved at *construction* time.
Preserving that timing is what keeps `monkeypatch.chdir`-based tests, and any
process that changes directory before constructing `Settings`, behaving exactly
as before.

**This second fix was required to keep two existing M6 tests passing** —
`test_documented_production_env_loads` and
`test_startup_validation_is_reachable_for_that_config` — which the naive first
attempt broke. They pass now.

---

## 3. Fresh-clone installation

Clean clone into a scratch directory, nothing shared with the working tree.

| Step | Command | Result |
| --- | --- | --- |
| Clone | `git clone …` | ✅ |
| Backend venv | `python3 -m venv .venv` | ✅ Python 3.11.2 |
| Backend deps | `pip install -r requirements.txt` | ✅ 36 packages, no compiler needed |
| Schema | `alembic upgrade head` | ✅ 8 revisions applied |
| Boot | `uvicorn app.main:app` | ✅ healthy in ~12 s |
| `/health` | | ✅ `200 {"status":"healthy"}` |
| `/health/live` | | ✅ `200` with uptime |
| `/health/ready` | | ✅ `200`, all checks `ok` |
| `/metrics` | | ✅ Prometheus exposition |
| Create + run a workflow | 3-node graph | ✅ `COMPLETED`, 3/3 nodes, 12.5 ms |
| Graceful shutdown | `SIGTERM` | ✅ scheduler stopped, clean exit |
| Frontend deps | `npm install` | ✅ (`ELECTRON_SKIP_BINARY_DOWNLOAD=1`) |
| Frontend typecheck | `tsc --noEmit` | ✅ clean |
| Frontend build | `vite build` | ✅ 1735 modules, 343.85 kB (109.08 kB gzip) |
| Frontend tests | `vitest run` | ✅ 179/179 in 13 files |

**Prerequisites a first-time user needs, and where they are now stated:**
Python 3.11+, Node 22+, ~500 MB disk. PostgreSQL, FFmpeg, Docker and Ollama are
all optional. This was previously scattered; it is now in `README.md` and
`docs/INSTALLATION_GUIDE.md`, which before M7 was 19 lines that pointed at
`scripts/build.sh` without stating a single prerequisite.

---

## 4. PostgreSQL

Real PostgreSQL **16.2** (embedded `pgserver` build), TCP on `127.0.0.1:55432`,
`max_connections=200`.

**The full backend suite was run against it: 1446/1446 passed, zero skips.**

This closes **M6-5**, which M6 had to leave open: the 8 PostgreSQL migration
regression tests skip unless `TEST_POSTGRES_URL` is set, and no PostgreSQL was
available in M5 or M6. They now execute and pass:

```
tests/m6/test_postgres_migrations_m6.py ........  [100%]
8 passed in 13.80s
```

Production posture verified against PostgreSQL with a real production `.env`:

| Check | Result |
| --- | --- |
| Migrations → 19 tables | ✅ |
| Boot in `ENVIRONMENT=production` | ✅ |
| `/health/ready` | ✅ `200`, `database: ok` |
| `/docs` | ✅ `404` (disabled) |
| Unauthenticated `/api/workflows/` | ✅ `401` |
| Host-header injection (`Host: evil.example.com`) | ✅ `400` |
| Bootstrap admin created | ✅ exactly one `admin` row |
| JWT login → authenticated call | ✅ `200` |
| `/api/system/config/validation` | ✅ 0 errors, 1 expected warning |
| JSON logs written | ✅ |
| **Secret redaction** | ✅ admin password and DB password appear **0 times** in the log file |

---

## 5. Operations

| Operation | Method | Result |
| --- | --- | --- |
| Graceful shutdown | `SIGTERM` | ✅ scheduler stopped, "Shutdown complete", clean exit |
| Restart | stop → start | ✅ healthy, **data intact** |
| Backup | `pg_dump \| gzip` | ✅ 5.5 kB archive |
| Disaster | `DELETE FROM workflows` | ✅ rows gone (0 remaining) |
| Restore | `gunzip \| psql` into a clean DB | ✅ **canary row and admin user recovered**, 0 errors |
| Rollback `-1` | `alembic downgrade -1` | ✅ clean |
| Roll forward | `alembic upgrade head` | ✅ clean — no `DuplicateObject` (M6-F3 holds) |
| Full downgrade | `alembic downgrade base` | ✅ **0 orphaned enum types**, 2 residual tables (`alembic_version` + APScheduler jobstore, both expected) |
| Re-upgrade from base | `alembic upgrade head` | ✅ 19 tables restored |
| Single migration head | `alembic heads` | ✅ exactly one |
| Log rotation | `RotatingFileHandler` | ✅ configured 10 MB × 5 backups; **rollover not triggered** (would need 10 MB of log) |
| Config validation | `/api/system/config/validation` | ✅ |
| Metrics | `/metrics` | ✅ |

---

## 6. Docker — **unverified**

**No container runtime is available in this environment, and none can be
installed.** This is the third milestone in a row (M5, M6, M7) in which the
Docker layer could not be executed.

Checked explicitly:

| Probe | Result |
| --- | --- |
| `docker`, `podman`, `nerdctl`, `buildah`, `img`, `containerd`, `runc`, `crun` | all absent |
| `/var/run/docker.sock` | does not exist |
| `registry-1.docker.io`, `ghcr.io`, `quay.io`, `public.ecr.aws`, `mirror.gcr.io` | unreachable |
| `download.docker.com` | unreachable (TLS handshake fails) |
| `apt-get install podman` | `E: Unable to locate package podman` |

### What this means

**Unverified:** `docker build` for both images; `docker compose up`; container
networking between `frontend`, `backend` and `db`; volume persistence across
`docker compose down/up`; the compose healthcheck wiring; container restart
policy; the containerised upgrade and rollback procedure.

**Mitigated:** every *process* the containers would run has been verified
outside a container — the same PostgreSQL 16, the same production settings, the
same Uvicorn command line, the same `/health/live` and `/health/ready` probes,
the same `alembic upgrade head` release step, the same backup and restore
commands.

**Newly added in M7:** `backend/tests/m7/test_docker_assets_m7.py` — 23 static
consistency tests over the Docker assets, covering the mistakes that would
otherwise surface only as a failed deployment:

- every `${VAR}` compose needs is documented in `.env.production.example`;
- `nginx` proxies to a hostname that is an actual compose service, on the port
  the backend actually binds;
- container probe paths (`/health/live`, `/health/ready`) are real routes —
  asserted against the live FastAPI route table, not a string;
- the `migrate` service is one-shot and profile-gated, and the `backend`
  service does **not** run migrations (which would race across replicas);
- mandatory secrets use `:?` so compose refuses to start rather than defaulting;
- the database is not published to the host;
- the backend image runs as an unprivileged user and is multi-stage;
- `.dockerignore` excludes `.env` and `*.db`.

These are text-level and route-level checks. **They are not a substitute for
running the stack**, and are not counted as Docker verification.

**Treat the first containerised deployment as a validation exercise.**

---

## 7. Examples

Four example workflows were added, and all four are **executed** by
`scripts/verify_examples.py` against a live backend — import → validate → run →
read back → export → assert round trip.

```
PASS  01-hello-automation.json       6n/6e   5 executed   14 ms
PASS  02-ai-content-pipeline.json    5n/4e   5 executed  249 ms
PASS  03-resilient-http-sync.json    7n/7e   5 executed  369 ms
PASS  04-scheduled-batch-report.json 5n/4e   5 executed  516 ms

4/4 examples passed
```

Writing the harness immediately paid for itself: the first run failed two
examples with `{{ Start.topic }}` templates that silently render **empty**. The
correct reference for a variable seeded by the `start` node is
`{{ Start.variables.topic }}`. Had the harness not existed, four broken example
files would have shipped in the release — exactly the failure mode that put 20
zero-byte node components into the M5 tree.

### Environment-dependent caveat (not a product defect)

Example 03 initially failed with `CERTIFICATE_VERIFY_FAILED` while `curl` to the
same URL returned `200`. Proven to be this sandbox's TLS-inspecting proxy: the
backend's `httpx` client validates against the `certifi` bundle, which lacks the
proxy's CA. The identical request succeeds when pointed at the system trust
store:

```python
httpx.get(url, verify=ssl.create_default_context(
    cafile="/etc/ssl/certs/ca-certificates.crt"))   # -> 200
```

Re-running the backend with `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`
made **4/4 examples pass**. Documented in `docs/TROUBLESHOOTING.md` and in the
example's own `tls_note`.

---

## 8. Repository hygiene

| Check | Result |
| --- | --- |
| `TODO` / `FIXME` / `XXX` / `HACK` in source | **none** |
| Broken internal doc links | **none** — every referenced `.md` resolves |
| Migration heads | single (`d5f3a7c81b64`) |
| Orphaned ORM tables without a migration | none (asserted by a test) |
| Secrets committed | none; `.env` is ignored, only templates are tracked |

### Stale or duplicate content found

| Item | Assessment |
| --- | --- |
| `release_notes.txt` (repo root) | **Stale.** Claims "Version 1.0.0 … ready for production" and "All 17 backend subsystem tests pass" against an actual 1487. Superseded by `docs/RELEASE_NOTES.md` |
| `docs/RELEASE_NOTES.md` | **Stale.** Described 0.3.0-alpha. Rewritten for 1.1.0-rc1 |
| `docs/V1_AUDIT_REPORT.md` | Historical V1.0 document asserting "production-ready" on 17 tests. Retained as history, but it contradicts current status |
| `docs/TODO.md` | Says "Version 1.0 is feature-complete"; its four "future" items are all now built |
| `docs/ROADMAP_PROGRESS.md` | All 15 items ticked including "Produce release candidate" — which was not true until this milestone |
| `docs/TEST_COVERAGE.md` | Counts are M4-era (1085/105) |
| `backend/main.py` | 15-line V1.0 stub that shadows `app.main:app`. **Verified still in use** by `tests/test_main.py`, so it is *not* dead code and was not removed. Flagged since M5 (A5) |
| `package-lock.json` (repo root) | Empty stub (`"packages": {}`) for a root `package.json` that does not exist. Harmless; not removed without a confirmed owner |
| `frontend/README.md` | Vite template boilerplate, not Creator OS content |

Per the M7 rule — *remove only verified dead code* — **nothing was deleted.**
`backend/main.py` is the one item that looks dead and is not.

---

## 9. Test results

| Suite | Result |
| --- | --- |
| Backend, SQLite | **1487 passed**, 8 skipped (PostgreSQL-gated), 0 failed |
| Backend, PostgreSQL 16.2 | **1446 passed**, **0 skipped**, 0 failed |
| Backend coverage | **89%** (7734 statements, 875 uncovered) |
| Frontend | **179 passed**, 13 files |
| Frontend typecheck | clean |
| Frontend production build | clean, 343.85 kB (109.08 kB gzip) |
| New in M7 | **46 tests** (23 config regression + 23 Docker static) |

The M7 regression tests were checked against the pre-fix code: 6 fail without
the M7-F2 fix, and the M7-F1 suite cannot even import. They are real guards, not
tautologies.

---

## 10. Remaining known limitations

Carried forward and re-confirmed, with the M7 position on each:

| # | Limitation | M7 status |
| --- | --- | --- |
| M6-1 | Docker never executed | **still open** — see §6 |
| M5-1 | Single-process execution; in-memory queue; queued runs lost on restart | still open, documented |
| M5-2 | Rate limiting is per-process | still open, documented |
| M5-3 | SSE broker is per-process | still open, documented |
| M5-4/5 | Script sandbox is defence in depth, **not** a security boundary; the JS node is unsandboxed | still open, both disabled by default |
| M5-6 | RBAC is global, not per-resource | still open |
| M5-7 | Access tokens cannot be revoked before expiry | still open |
| M5-11 | CI has never run | still open — needs a maintainer to move `ci/github-actions-ci.yml` into `.github/workflows/` |
| M6-3 | SSE `cleanup()` can drop a concurrent subscriber's replay buffer | still open, benign |
| M6-4 | Two timing-sensitive tests can flake under CPU contention | **not observed** in any M7 run |
| M6-6 | Rare `psycopg` C-extension segfault | **not observed** in M7's PostgreSQL runs |
| **M6-5** | PostgreSQL migration tests skip without `TEST_POSTGRES_URL` | ✅ **closed in M7** — executed and passing |

New, environment-specific, not product defects:

| Item | Note |
| --- | --- |
| TLS interception breaks outbound HTTPS from workflow nodes | Set `SSL_CERT_FILE` to the system CA bundle. Proven environmental (§7) |
| Log rotation rollover not exercised | Handler is configured correctly; triggering it needs 10 MB of log output |
| Electron desktop shell not launched | Binary download is skipped in this environment; the browser build, tests and typecheck all pass |

---

## 11. Readiness

**88%.**

| Dimension | Weight | Score | Why |
| --- | --- | --- | --- |
| Source installation | 20% | 100% | Fresh clone verified end to end |
| PostgreSQL deployment | 20% | 100% | Full suite + production posture verified |
| Operations (backup, restore, restart, rollback) | 15% | 95% | All verified; log rollover not triggered |
| Docker deployment | 20% | 25% | Assets statically validated; **never executed** |
| Documentation | 15% | 95% | Rewritten and verified against the running system |
| Examples | 10% | 100% | 4/4 executed against a live backend |

Not 95%+, and deliberately so: **one of the five documented deployment paths has
never been run.** A percentage above that would be an assertion, not a
measurement. Verifying Docker on any machine with a container runtime is the
single highest-value action remaining, and it is the only thing standing between
this build and a genuine ≥95%.
