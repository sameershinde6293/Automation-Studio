# M9 — Production Staging & Real-World Validation

Creator OS v1.1.0-rc3 · executed 2026-07-28
Branch `arena/019fa483-automation-studio` · base `6054def` (PR #10 merge)

M9 is not a feature milestone. Its purpose was to run Creator OS on a
production-shaped environment, measure what it actually does, break it on
purpose, and fix only what the evidence showed to be broken.

**Headline:** the staging deployment ran on **real PostgreSQL 16.2** for the
first time in this project's history. Every previous milestone (M5–M8) either
skipped the PostgreSQL suite or ran it once against a scratch database; M9 ran
the whole system — migrations, auth, workflow execution, failure injection,
backup and restore — against a live server. That unlocked eight tests that had
been skipped since M6 and exposed **four real defects**, three of them in code
or scripts that earlier milestones had marked "verified".

The Docker runtime remains **unexecuted**. That limitation is unchanged and is
stated plainly in §9.

---

## 1. Environment

| | |
| --- | --- |
| Host | Debian 12 (bookworm), Linux 6.1.158, x86_64 |
| CPU / RAM | 2 cores / 3.9 GB, 21 GB disk |
| Python | 3.11.2 |
| Node | 22.22.3 |
| **PostgreSQL** | **16.2, real server, TCP 127.0.0.1:5433** (via the `pgserver` wheel) |
| Container runtime | **none** — see §9 |

### 1.1 How PostgreSQL became available

M5–M8 all reported "no PostgreSQL available" and skipped
`tests/m6/test_postgres_migrations_m6.py`. That conclusion was true for `apt`
(Debian mirrors are unreachable from this sandbox) but incomplete: **PyPI is
reachable**, and the `pgserver` wheel bundles a complete PostgreSQL 16.2
distribution — `postgres`, `initdb`, `psql`, `pg_dump`, `pg_isready`, the lot.

```
$ pip install pgserver
$ pg_ctl -D /tmp/pgdata -o "-h 127.0.0.1 -p 5433" start
$ psql "postgresql://creator@127.0.0.1:5433/creator_os" -c "select version()"
 PostgreSQL 16.2 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 10.2.1, 64-bit
```

This is the same major version the compose file pins (`postgres:16-alpine`),
so the SQL surface, the enum behaviour that caused M6-F3, and the migration
path are all genuinely exercised rather than approximated by SQLite.

### 1.2 Staging deployment

A production-shaped instance, not a test harness:

- `ENVIRONMENT=production`, `AUTH_ENABLED=true`, `ENABLE_DOCS=false`
- PostgreSQL over TCP, pool 20 + 60 overflow
- JSON logs to a file, HSTS, CSRF, trusted hosts, rate limiting on
- Bootstrap admin created on first boot, then cleared and restarted
- Schema applied with `alembic upgrade head` (8 revisions, 20 tables)

```
{"status":"ready","version":"1.1.0-rc3","checks":{"database":"ok",
 "scheduler":"ok","execution_workers":"ok","queue_depth":0,
 "configuration":"ok"}}
```

A second instance (`:8801`) ran with rate limits raised so that load testing
measured the application rather than the rate limiter, and a third
(`failure_test` database) was used for destructive testing so the long-run
instance was never disturbed.

---

## 2. Findings

Four defects, all found by executing something rather than reading it.

| ID | Severity | Area | Status |
| --- | --- | --- | --- |
| M9-F1 | High | Database pool saturation invisible in `/metrics` | **Fixed** |
| M9-F2 | Medium | Account lockout never written to the audit trail | **Fixed** |
| M9-F3 | **Critical** | `backup.sh` reports success while producing no database backup | **Fixed** |
| M9-F4 | Low | Shipped version disagreed with published version | **Fixed** |
| M9-F5 | Low | README test counts stale | **Fixed** |

### M9-F1 — Pool saturation was invisible at run time (High)

`docs/DEPLOYMENT.md` and `app/core/startup.py` both state that pool capacity
(`DB_POOL_SIZE + DB_MAX_OVERFLOW`) is what caps request concurrency, because
every in-flight request holds a connection for its whole lifetime. M6 load
testing measured exactly that. But `/metrics` exported **no pool metric at
all** — the 8 exported metric families covered HTTP, executions and auth only.

Why it matters: pool exhaustion presents as requests blocking on checkout for
`DB_POOL_TIMEOUT_SECONDS`. From outside, that is indistinguishable from a slow
database. The one number that tells them apart was not collected.

**Fix.** Six gauges, refreshed on scrape next to the existing queue gauges:

```
creator_os_db_pool_size 20
creator_os_db_pool_checked_out 0
creator_os_db_pool_available 0
creator_os_db_pool_overflow -20
creator_os_db_pool_capacity 80
creator_os_db_pool_utilisation_ratio 0
```

Verified live under 40 concurrent clients — the gauges move:

```
peak checked_out: 40.0
peak utilisation ratio: 0.5
```

The refresh is wrapped so a broken pool can never break a scrape
(`test_metrics_scrape_survives_a_broken_pool`).

### M9-F2 — Account lockout produced no audit event (Medium)

Driving 25 rapid bad logins against staging locked the account correctly
(`failed_login_count=5`, `locked_until` set). The audit table afterwards:

```
 event_name           | count
----------------------+-------
 auth.login.succeeded |    43
 auth.login.failed    |    10
```

No lockout record. The lockout existed only as a `logger.warning`, so anything
consuming the audit trail — the enterprise audit views, or a SIEM export — saw
a run of failures and no indication that the account was actually locked. That
is precisely the event a security reviewer looks for.

**Fix.** `auth.account.locked` is now emitted after the lockout commits, with
username, failure count, `locked_until` and the configured lockout window.
Verified end to end against PostgreSQL:

```
 event_name          | count
---------------------+-------
 auth.login.failed   |     6
 auth.account.locked |     1

details | {"username": "lockadmin", "failed_login_count": 5,
           "locked_until": "2026-07-28T08:27:47", "lockout_seconds": 900.0}
```

Auditing is best-effort by design: a broken audit sink must not deny logins,
which is covered by `test_audit_failure_does_not_break_authentication`.

### M9-F3 — Backup reported success while backing up nothing (Critical)

This is the most serious finding of the milestone. Running the shipped
`scripts/backup.sh` against the PostgreSQL staging deployment:

```
=== Backup completed: backups/test1 ===
$ ls backups/test1/
env.sanitized  manifest.txt  media.tar.gz          <-- no database
$ echo $?
0
```

Exit status 0. A cron job would report success forever. The failure only
becomes visible during a restore — that is, during an incident.

Four distinct causes, all found by executing the script:

1. **Missing `pg_dump` was a warning, not an error.** The branch printed
   `⚠ pg_dump not available` and fell through to a successful exit.
2. **`pg_dump` failure was swallowed** by `|| echo "⚠ pg_dump failed"`.
3. **The connection was rebuilt from `POSTGRES_USER`/`POSTGRES_DB`**, discarding
   host and port from `DATABASE_URL`. Even with `pg_dump` present it would have
   dumped whatever local cluster answered, not the configured one.
4. **Media backup ignored `MEDIA_ROOT`**, archiving an empty
   `backend/media_storage` while the real media lived elsewhere. The resulting
   archive contained one empty directory entry.

`restore.sh` mirrored the same class of defect: it piped into
`psql "${DATABASE_URL:-}"` — empty unless the variable happened to be exported,
in which case psql silently targets the local socket — without
`ON_ERROR_STOP`, so a half-applied dump still exits 0.

**Fix.** Both scripts now fail loudly and target the right database:

- missing/failing `pg_dump` → `exit 1`, never a warning
- connection taken from `DATABASE_URL`, with the SQLAlchemy `+psycopg` suffix
  translated for libpq, and password redacted in output
- falls back to the `pgserver`-bundled `pg_dump`/`psql` when the host has no
  PostgreSQL client package
- archive verified with `gunzip -t` and a minimum-size check
- `sha256sum` of every artefact recorded in the manifest
- a backup directory with no database archive is a hard error
- restore uses `ON_ERROR_STOP=1`; a partial restore is a failure
- both sides honour `MEDIA_ROOT`

**Verified by a real disaster-recovery drill** (§7).

### M9-F4 / M9-F5 — Release metadata drift (Low)

README and `PROJECT_STATUS.md` advertised **1.1.0-rc2**, while
`backend/app/version.py`, `frontend/package.json` and the live
`/health/ready` response all reported **1.1.0-rc1**. A user could not tell
which artefact they were running. README also quoted "1529 passed / 8 skipped"
where the suite actually produced 1527 / 10.

**Fix.** Everything is now **1.1.0-rc3**, and
`tests/m9/test_release_consistency_m9.py` makes future drift a test failure:
backend, settings, `package.json`, `package-lock.json`, README headline,
PROJECT_STATUS and the running `/health/ready` payload must all agree.

---

## 3. Long-run validation (Phase 2)

A monitor sampled the process every 30 s while a workload driver executed the
`01-hello-automation` example continuously — synchronously and through the
queue — for the whole window.

| Metric | Result |
| --- | --- |
| Duration | **31 minutes** continuous (see limitation below) |
| Samples | 63 process/DB samples, 93 workload cycles |
| Workflow executions | **312 rows**, all terminal |
| Workflow failures | **0 sync, 0 queued** |
| `/health/live` non-200 | **0** |
| `/health/ready` non-200 | **0** |
| RSS | 91.2 MB → 98.1 MB (peak HWM 98.1 MB) |
| CPU | 18.5 s over 1862 s = **1.0 % of one core** |
| Threads / FDs | 3 → 6 / 14 → 15 (bounded, no leak) |
| DB size | 8.7 MB → 10.7 MB (312 executions of real data) |
| Log growth | 2.5 KB → 255 KB |
| `/health/live` latency | median 3.99 ms, max 13.55 ms |
| `/health/ready` latency | median 4.11 ms, max 19.48 ms |
| Sync execution | median 58.4 ms, max 89.2 ms |
| Queued completion | median 280.4 ms, max 306.0 ms |

**Memory.** +6.9 MB over 31 minutes while inserting 312 executions is
consistent with working-set growth (SQLAlchemy identity maps, the in-memory
error-aggregation ring buffer sized at 500, Python arena fragmentation) rather
than an unbounded leak: RSS tracked database growth and then flattened, and FDs
and threads stayed bounded. It is **not** proof of 24-hour stability — see the
limitation below.

**Connection pooling.** One sample recorded 47 connections in
`idle in transaction`. That was traced to the concurrent pool-probe running at
that moment, not to a leak: a controlled re-test (480 requests across 60
threads) settled to a steady 25 connections with **`idle_in_txn = 0`**, and it
stayed there across 30 s of observation.

> **Limitation — this is 31 minutes, not 24 hours.** The milestone asks for
> "24-hour stability (or as long as the environment allows)". This sandbox does
> not persist across the session, and an earlier reset in this very session
> destroyed a running long-run test. Slow leaks (a few MB/hour), log rotation
> at the 10 MB boundary, `pool_recycle` at 1800 s and scheduler drift over many
> cycles are **not** covered by a 31-minute window. Treat the first production
> soak as the real 24-hour test.

---

## 4. Observability (Phase 3)

| Item | Result |
| --- | --- |
| `/health` | 200 `{"status":"healthy"}` |
| `/health/live` | 200 with `uptime_seconds`; **no DB dependency** — stays 200 when PostgreSQL is stopped |
| `/health/ready` | 200 `ready` / **503 `degraded`** with a per-dependency breakdown |
| `/metrics` | Prometheus exposition, **14 metric families** (8 before M9 + 6 new pool gauges) |
| Structured logs | JSON, one object per line, with `request_id` and `correlation_id` |
| Correlation IDs | Present on every request log line and echoed in error bodies |
| Audit log | Persisted to PostgreSQL: `auth.login.succeeded`, `auth.login.failed`, and now `auth.account.locked` |
| Backup / restore | Executed against real PostgreSQL — §7 |

Sample log line from the staging run:

```json
{"ts":"2026-07-28T08:07:19+0000","level":"INFO","logger":"creator_os.http",
 "message":"GET /health/ready -> 200 in 4.80ms",
 "request_id":"b633660ec07d4b94af8ed63e57b547e5",
 "correlation_id":"b633660ec07d4b94af8ed63e57b547e5",
 "method":"GET","path":"/health/ready","status_code":200,"duration_ms":4.8}
```

Observability survives dependency loss: with PostgreSQL stopped, `/metrics`
still returned **200**.

---

## 5. Performance (Phase 4)

Measured against the live staging instance on 2 cores. All requests succeeded.

### API latency (ms)

| Endpoint | n | p50 | p95 | p99 | max | errors |
| --- | --- | --- | --- | --- | --- | --- |
| `GET /health` | 300 | 2.38 | 2.91 | 3.37 | 3.79 | 0 |
| `GET /health/live` | 300 | 2.38 | 2.79 | 3.19 | 3.43 | 0 |
| `GET /health/ready` | 300 | 3.14 | 3.75 | 4.82 | 7.11 | 0 |
| `GET /metrics` | 100 | 2.68 | 4.57 | 5.01 | 7.02 | 0 |
| `GET /api/workflows/` | 300 | 7.01 | 8.44 | 9.55 | 71.46 | 0 |
| `GET /api/executions/` | 300 | 10.58 | 13.82 | 17.04 | 65.13 | 0 |
| `GET /api/executions/queue` | 300 | 4.14 | 4.78 | 5.11 | 6.76 | 0 |
| `GET /api/executions/stats` | 100 | 6.61 | 7.47 | 9.09 | 10.66 | 0 |
| `POST /api/auth/login` | 20 | 210.38 | 216.54 | 220.43 | 221.40 | 0 |

Login is ~210 ms **by design**: PBKDF2 at 600 000 iterations. That is a
deliberate cost, not a bottleneck to optimise.

### Workflow execution

Synchronous run of the 6-node `01-hello-automation` example, n=50:
**p50 57.3 ms, p95 68.0 ms, p99 76.8 ms, max 77.5 ms**, 50/50 `COMPLETED`.

### Concurrency

| Scenario | n | p50 | p95 | max | rps | errors |
| --- | --- | --- | --- | --- | --- | --- |
| concurrent workflow exec ×5 | 10 | 270.95 | 276.73 | 276.87 | 18.21 | 0 |
| concurrent workflow exec ×10 | 20 | 489.46 | 681.69 | 723.06 | 18.27 | 0 |
| concurrent workflow exec ×20 | 40 | 937.41 | 1092.96 | 1093.65 | 19.84 | 0 |
| `GET /api/workflows/` ×10 | 50 | 76.90 | 170.66 | 193.17 | 105.10 | 0 |
| `GET /api/workflows/` ×50 | 250 | 395.13 | 482.44 | 501.21 | 118.95 | 0 |
| `GET /api/workflows/` ×100 | 500 | 973.01 | 1093.64 | 1111.28 | 101.77 | 0 |

Execution throughput saturates at ~18–20 rps regardless of client count, which
is the expected shape for a 4-worker in-process pool on 2 cores: latency grows
linearly, throughput stays flat, and **nothing is shed or dropped**. Read
throughput plateaus near ~100–120 rps with zero errors at 100 concurrent
clients — the pool (capacity 80) was never exhausted, which the new M9-F1
gauges now make observable.

### Startup / shutdown (6 runs)

| | min | median | max |
| --- | --- | --- | --- |
| Startup to `/health/live` | 1069 ms | 1098 ms | 1272 ms |
| Startup to `/health/ready` | 1091 ms | 1120 ms | 1296 ms |
| Graceful shutdown (SIGTERM) | 186 ms | 188 ms | 189 ms |

Graceful shutdown with 5 queued executions in flight took **5.1 s** and left
**0 executions RUNNING and 0 QUEUED** — the pool drained rather than abandoning
work.

**No optimisation was performed.** Every measured number is comfortably inside
its budget, and the engineering rule is to optimise only measured bottlenecks.

---

## 6. Failure testing (Phase 5)

All scenarios executed against a live stack on a dedicated database.

| Scenario | Result |
| --- | --- |
| **Database stopped under a live backend** | `/health/ready` → **503 `degraded`** with `"database":"error: OperationalError"`; `/health/live` stays **200**; `/metrics` stays **200**; process survives |
| **Database recovery** | `/health/ready` back to **200 in 1 s**, authenticated API 200, **no restart required** |
| **Unreachable DB at boot** | Process boots and serves; live **200**, ready **503** — correct "up but not in rotation" |
| **SIGKILL (crash)** | Port dead; after restart all committed data intact; **0 orphaned RUNNING executions** |
| **SIGTERM with 5 queued executions** | Drains in 5.1 s, `Job scheduler stopped` → `Shutdown complete.`, 0 RUNNING / 0 QUEUED left behind |
| **Partial workflow failure** | Execution `FAILED`, failing node recorded with its error, **downstream node never ran**, 4 durable log records, 2 timeline entries, `resume-failed` → HTTP 201 new execution with parent lineage |
| **Network interruption** | Unreachable peer bounded by node timeout (0.1 s, guard fires early); half-open stall bounded at 3.0 s by client timeout; backend healthy, worker pool still running |
| **Disk full** (tmpfs, ~150 KB free) | 500 KB upload → clean **HTTP 500** with a correlation ID and `OSError: [Errno 28] No space left on device` in the log; `/health/live` and `/health/ready` stay **200**; **no crash** |
| **Invalid configuration** | 6 of 7 unsafe configurations **refused to boot** (see below) |

### Configuration validation

| Configuration | Outcome |
| --- | --- |
| empty `AUTH_SECRET_KEY` | **REFUSED** |
| 5-character `AUTH_SECRET_KEY` | **REFUSED** — "brute-forceable" |
| `AUTH_ENABLED=false` in production | **REFUSED** |
| `CORS_ORIGINS=*` with credentials | **REFUSED** |
| `HTTP_EXECUTOR_ALLOW_PRIVATE_NETWORKS=true` | **REFUSED** — SSRF |
| shell executor enabled with empty allowlist | **REFUSED** |
| unreachable `DATABASE_URL` | **Boots** (by design) |

The last row is correct behaviour, not a defect: static validation does not
open a connection, so an unreachable database is caught by the readiness probe
(503) instead of blocking startup. That is what lets a deployment survive
starting before its database does.

---

## 7. Backup and restore — real disaster-recovery drill

Executed end to end against PostgreSQL after the M9-F3 fix.

```
1. Backup      ./scripts/backup.sh
               ✓ database.sql.gz (12K), media.tar.gz, manifest with SHA256
               source: users=1 workflows=1 executions=14 nodes=6

2. Disaster    createdb restore_drill        (empty: 0 tables)

3. Restore     ./scripts/restore.sh <dir>
               Target: postgresql://creator:***@127.0.0.1:5433/restore_drill
               ✓ Database restored   ✓ Media restored

4. Verify      tables=20  users=1  workflows=1  executions=14  nodes=6
               alembic_version = d5f3a7c81b64   (migration state preserved)

5. Prove       backend booted against the restored database
               /health/ready → 200 ready, database ok
               login with restored credentials → 200 + access token
```

The dump contains real schema and data (40 `CREATE TABLE`/`COPY` statements,
including the PBKDF2 password hash that makes the restored login work).

The failure path was proved too — with `pg_dump` unavailable the script now
exits **1** instead of 0:

```
❌ pg_dump failed - no database backup was produced.
EXIT=1
```

---

## 8. Security validation (Phase 6)

| Attack | Result |
| --- | --- |
| No token | **401** |
| Garbage token | **401** |
| `alg=none` forged token | **401** |
| Tampered payload (role→admin, sub→999) with original signature | **401** |
| Expired `exp` claim | **401** |
| `Host: evil.example.com` | **400** (TrustedHost) |
| Malformed JSON body | **422**, structured error |
| Missing required field | **422** |
| Type-confused path parameter | **422** |
| Nonexistent resource | **404** |
| SQL injection in `?search=` | **200, 0 rows** — parameterised; `'; DROP TABLE workflows;--` left the table intact (9 rows after) |
| Declared 30 MiB body (limit 25 MiB) | **413** `payload_too_large` |
| Auth rate-limit abuse (25 rapid bad logins, limit 10/60 s) | 10 × 401 then **15 × 429** with `Retry-After: 58`, `X-RateLimit-Limit: 10`, `X-RateLimit-Remaining: 0` |
| Account lockout | Triggered at 5 failures, `locked_until` set, **now audited** (M9-F2) |
| `/docs` in production | **404** |
| Unauthenticated API | **401** |
| SSRF to loopback / private ranges from a workflow node | **Blocked** — `SecurityError: Requests to private/loopback address … are blocked` |

Note on the SSRF guard: it blocks `192.0.2.0/24` and `203.0.113.0/24` because
Python's `ipaddress` classifies the reserved documentation ranges as
`is_private`. That is conservative in the right direction — it cost this
milestone a cleaner network-timeout test, and it is recorded here so the
behaviour is not mistaken for a bug later.

---

## 9. Docker — still not executed

Unchanged from M5–M8, and stated plainly rather than worked around.

```
docker, podman, nerdctl, containerd, ctr : not installed
/var/run/docker.sock                     : absent
registry-1.docker.io, mirror.gcr.io,
public.ecr.aws, quay.io,
download.docker.com                      : TLS connection fails (000)
deb.debian.org                           : unreachable, apt cannot install
```

**Therefore not verified:** image build, image size, `compose config`/`up`,
container networking, volume persistence across `down`/`up`, in-container
HEALTHCHECK execution, restart policies, containerised upgrade/rollback,
json-file log rotation, and enforcement of the `cpus`/`memory` limits.

**Verified statically:** `scripts/docker_validate.sh` — **44 checks, 0 failures,
0 warnings**; `tests/m7/test_docker_assets_m7.py` (23) and
`tests/m8/test_docker_assets_m8.py` (30).

**Mitigated by execution outside containers:** the same PostgreSQL 16.2, the
same production settings, the same Uvicorn command line, the same
`alembic upgrade head` release step, the same liveness/readiness probes, and
now the same backup/restore commands have all been run for real.

The first containerised deployment must still be treated as a validation
exercise. Use `scripts/deploy.sh`.

---

## 10. Test results (Phase 8)

| Suite | Result |
| --- | --- |
| Backend, SQLite | **1562 passed, 10 skipped** (was 1527/10; +35 M9 tests) |
| Backend, with PostgreSQL | **1570 passed, 2 skipped** — the 8 M6 PostgreSQL migration tests **now execute** |
| PostgreSQL migration suite | **8/8 passed** (skipped in M6, M7 and M8) |
| Frontend | **179 passed** (13 files) |
| Typecheck | clean |
| Production build | clean — 343.85 kB (109.08 kB gzip) |
| Examples | **4/4 executed** against the live backend |
| Docker static validation | 44 checks, 0 failures |

The two remaining skips are the CI-workflow tests, which require
`.github/workflows/ci.yml` to be present — see §11.

### Examples

```
PASS  01-hello-automation.json       6n/6e   5 executed
PASS  02-ai-content-pipeline.json    5n/4e   5 executed   329 ms
PASS  03-resilient-http-sync.json    7n/7e   5 executed   459 ms
PASS  04-scheduled-batch-report.json 5n/4e   5 executed   548 ms
4/4 examples passed
```

Example 03 reaches the public internet and fails with
`CERTIFICATE_VERIFY_FAILED` unless `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`
is exported — a property of this sandbox's TLS interception, already documented
in M8, reconfirmed here.

---

## 11. Remaining limitations

Stated honestly; none of these are claimed as validated.

1. **Docker runtime has never been executed** — no runtime, no reachable
   registry. The single largest gap.
2. **Long run was 31 minutes, not 24 hours.** Slow leaks, log rotation at the
   10 MB boundary, `pool_recycle` at 1800 s and multi-hour scheduler drift are
   unproven.
3. **Single-node only.** No multi-replica run, no PgBouncer, no load balancer.
   `WEB_CONCURRENCY>1` and its effect on the per-worker rate limiter and
   per-worker execution queue remain untested.
4. **`.github/workflows/ci.yml` still cannot be pushed** by this app
   (`workflows` permission). `ci/github-actions-ci.yml` holds the same content;
   a maintainer must copy it. 2 tests stay skipped until then.
5. **No TLS termination executed.** nginx/Caddy configs are validated
   statically; neither has served a request here (no nginx binary).
6. **FFmpeg absent**, so media transcoding paths run only in their
   graceful-degradation form.
7. **2-core sandbox.** The throughput ceilings in §5 are properties of this
   host, not of the software.
8. **A sandbox reset destroyed the first long-run test mid-flight.** All
   evidence was regenerated afterwards; the numbers here come from the second,
   complete run.

---

## 12. Production readiness

**94 %** (M8: 92 %).

Raised two points for real, evidence-backed reasons:

- the whole system now runs on **real PostgreSQL 16.2**, and the migration
  suite that had been skipped since M6 **executes and passes**
- **backup and restore have actually been performed**, including a full
  disaster-recovery drill that ends with the application serving traffic from
  the restored database
- failure, security and performance behaviour is **measured**, not asserted
- a **critical** backup defect that would have caused silent data loss is fixed

Not higher, and deliberately **not above 98 %**, because the milestone's own
rule is explicit: no deployment path may be claimed without execution. The
Docker path — which is the documented production deployment path — has still
never been run. Multi-replica operation is untested, and 31 minutes is not a
24-hour soak.

To reach ≥98 %, someone must run, on a host with Docker:

```bash
cp .env.production.example .env      # set AUTH_SECRET_KEY, POSTGRES_PASSWORD
docker compose --profile tools run --rm migrate
docker compose up -d
curl -fsS http://localhost:8080/health/ready
./scripts/backup.sh && ./scripts/restore.sh backups/<ts>
docker compose down && docker compose up -d   # persistence across recreate
```

plus a 24-hour soak and a two-replica run behind a load balancer.
