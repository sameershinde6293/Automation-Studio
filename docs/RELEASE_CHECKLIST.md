# Release, Installation, Deployment & Operations Checklists

Creator OS v1.1.0

Five checklists, for five different people. Each item is either something this
project verified in the M10 certification run (marked ✅ with the evidence) or
something **you** must do on your own infrastructure (an empty box).

Evidence for every ✅: [`M10_RELEASE_CERTIFICATION.md`](M10_RELEASE_CERTIFICATION.md).

---

## 1. Release checklist (maintainer, cutting v1.1.0)

### Verified in M10

- [x] Working tree clean, branched from the merged PR #11 commit `f7fc410`
- [x] Version identical in `backend/app/version.py`, `frontend/package.json`,
      `frontend/package-lock.json`, README, `PROJECT_STATUS.md`, doc headers
      and the live `/health/ready` payload — enforced by
      `tests/m9/test_release_consistency_m9.py` and `tests/m10/`
- [x] Backend tests: **1576 passed / 10 skipped** (SQLite)
- [x] Backend tests: **1584 passed / 2 skipped** (PostgreSQL 16.2)
- [x] Backend coverage **89%**
- [x] Frontend: **179 passed**, `tsc --noEmit` clean, production build clean
- [x] Examples **4/4** against an authenticated production backend
- [x] `CHANGELOG.md` and `RELEASE_NOTES.md` lead with `1.1.0`
- [x] `KNOWN_ISSUES.md` reflects reality, including what was **not** executed
- [x] No `TODO`/`FIXME`/debug code in shipped source
- [x] 0 broken relative documentation links

### Still to do at cut time

- [ ] Tag and push the release:
      ```bash
      git tag -a v1.1.0 -m "Creator OS v1.1.0 — General Availability"
      git push origin v1.1.0
      ```
- [ ] Create the GitHub release, body from `docs/RELEASE_NOTES.md` §v1.1.0
- [ ] **Add a LICENSE file** — absent today, so all rights are reserved
- [ ] **Activate CI** (`cp ci/github-actions-ci.yml .github/workflows/ci.yml`)
      and confirm the 7 jobs go green — never yet executed
- [ ] Build and push container images once a runtime is available — the Docker
      path has never been executed

---

## 2. Installation checklist (first-time user, local desktop)

- [ ] Python 3.11+, Node 22+, ~500 MB free disk
- [ ] `git clone` and `cd Automation-Studio`
- [ ] `cd backend && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt`
- [ ] `./.venv/bin/alembic upgrade head`
- [ ] Start with **`app.main:app`**, never `main:app`
      (`backend/main.py` is a V1.0 stub with no routers)
- [ ] `curl http://localhost:8000/health` → `{"status":"healthy"}`
- [ ] `cd frontend && ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm install && npm run dev`
- [ ] Open http://localhost:5173
- [ ] `python scripts/verify_examples.py` → 4/4
- [ ] Behind a TLS-inspecting proxy, export
      `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` **for the backend
      process** — not for the verifier (M10-F1)

Full walkthrough: [`INSTALLATION_GUIDE.md`](INSTALLATION_GUIDE.md).

---

## 3. Deployment checklist (operator, server install)

### Before

- [ ] `cp .env.production.example .env`
- [ ] Set `AUTH_SECRET_KEY` (long random) and `POSTGRES_PASSWORD` — both mandatory
- [ ] Set `ENVIRONMENT=production`, `AUTH_ENABLED=true`, `ENABLE_DOCS=false`
- [ ] Set `ALLOWED_HOSTS` and `CORS_ORIGINS` to your real hostnames
- [ ] Size `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` under PostgreSQL `max_connections`
- [ ] Keep `WEB_CONCURRENCY=1` — the queue, rate limiter and SSE broker are
      per-process (unchanged since M5)
- [ ] Plan TLS termination in front (nginx or Caddy configs in `deploy/`)

### Schema first, then code

- [ ] Back up before touching anything (checklist 4)
- [ ] `alembic upgrade head` **before** starting new code
- [ ] Source path: `systemctl start creator-os` (unit in `deploy/systemd/`)
- [ ] Docker path: `docker compose --profile tools run --rm migrate` then
      `docker compose up -d` — ⚠️ **never executed by this project; treat your
      first run as a validation exercise** and use `scripts/deploy.sh`

### Verify after

- [ ] `/health/live` → 200
- [ ] `/health/ready` → 200 with `database ok`, `scheduler ok`, `execution_workers ok`
- [ ] `/docs` → **404** (docs disabled in production)
- [ ] Unauthenticated API call → **401**
- [ ] Request with a spoofed `Host` → **400**
- [ ] `/metrics` returns the 14 metric families, including `creator_os_db_pool_*`
- [ ] Grep the log file for a known secret → **0 hits**
- [ ] Create the first admin via `AUTH_BOOTSTRAP_USERNAME` /
      `AUTH_BOOTSTRAP_PASSWORD`, log in, then **clear both and restart**
- [ ] `nginx -t` on the host before cutting over (never executed here)

---

## 4. Operational checklist (day two)

### Daily / automated

- [ ] `scripts/backup.sh` on a schedule. It now **fails loudly** rather than
      writing an empty backup (M9-F3) — alert on non-zero exit
- [ ] Confirm `database.sql.gz` is present and non-trivial in each backup
- [ ] Scrape `/metrics`; alert on `creator_os_db_pool_utilisation_ratio`
      approaching 1.0 — pool exhaustion looks exactly like a slow database
- [ ] Alert on `/health/ready` returning 503 while `/health/live` returns 200

### Periodically

- [ ] **Rehearse a restore.** `scripts/restore.sh <dir>` on staging, then confirm
      the app authenticates against the restored database. This project ran the
      full drill (dump → `DROP SCHEMA CASCADE` → restore → 200) — rehearse it on
      *your* infrastructure
- [ ] Verify `alembic current` matches the deployed code's head
- [ ] Check log rotation is actually rolling over at 10 MB — configured but
      **never triggered** in validation
- [ ] Review the audit trail (`auth.*` events, including `auth.account.locked`)
- [ ] Rotate `AUTH_SECRET_KEY` on a schedule (invalidates all sessions)

### Incident response

- [ ] **Database down** → readiness 503, liveness 200. The app recovers on its
      own within ~1 s of the database returning; **do not restart it**
- [ ] **Rollback** → `scripts/rollback.sh v1.1.0`, or `alembic downgrade -1` for
      a migration-only rollback. Downgrade to base leaves 0 orphaned enum types
- [ ] **Hard kill** → no orphaned executions are left behind, but **queued runs
      are lost** (in-memory queue); re-trigger them after restart

---

## 5. Administrator checklist (security & access)

- [ ] First admin bootstrapped, password changed, bootstrap variables cleared
- [ ] `AUTH_ENABLED=true` confirmed in the running config, not just the file —
      a mis-resolved `.env` silently disabled auth before M7 (M7-F1)
- [ ] `AUTH_ACCESS_TOKEN_TTL_SECONDS` set to your tolerance: **access tokens
      cannot be revoked before expiry** (default 15 min)
- [ ] API keys issued with the narrowest scope; scopes intersect the owner's role
- [ ] Understand that **RBAC is global, not per-resource** — any `editor` can
      modify any workflow. Do not use one instance to isolate mutually
      untrusting users
- [ ] Python/JavaScript script nodes left **disabled** unless every author is
      trusted. The Python sandbox is defence in depth, **not** a security
      boundary; the JavaScript node is not sandboxed at all
- [ ] Rate limiting terminated at a proxy if you run more than one process —
      the in-process limiter multiplies by the number of workers
- [ ] Uploads scanned out of band — there is no antivirus or archive-bomb check
- [ ] Audit coverage understood: auth and user administration are audited;
      workflow and media mutations are **not**
- [ ] Accept that **no external security review or penetration test** has been
      performed

Detail: [`SECURITY.md`](SECURITY.md) · [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md)
