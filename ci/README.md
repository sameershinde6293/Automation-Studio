# CI configuration

`github-actions-ci.yml` is the Creator OS continuous-integration pipeline:

| Job | What it runs |
| --- | --- |
| `backend` | `pytest` with coverage (1446 tests) |
| `migrations` | Alembic upgrade → downgrade → re-upgrade round trip on SQLite, and a single-head check |
| `migrations-postgres` | **Added in M6.** The same round trip against a real PostgreSQL 16 service container, plus the M6 pool/regression suite |
| `frontend` | `tsc --noEmit`, `vite build`, `vitest` (179 tests) |
| `docker` | Builds both production images |

## Why `migrations-postgres` exists

PostgreSQL is the only supported production database, but every migration test
before M6 ran on SQLite. SQLite has no native `ENUM` type, so it could not
reveal M6-F3: `downgrade` left PostgreSQL enum types behind, which wedged the
rollback procedure documented in `DEPLOYMENT.md`.

The M6 PostgreSQL tests **skip themselves** when `TEST_POSTGRES_URL` is unset
or unreachable, so that the suite stays runnable on a laptop. Without a
dedicated job they would silently never run in CI and the regression would be
unguarded — so the job ends with an assertion that parses the JUnit report and
**fails the build if every test skipped**. (That assertion is deliberately not
a `grep` over console output: `-q` suppresses the summary line, so a naive
`grep skipped` passes a fully-skipped run. This was observed while writing it.)

## Activating it — REQUIRES A MAINTAINER

**The pipeline has never executed.** The file lives in `ci/` rather than
`.github/workflows/`, and GitHub only runs workflows from the latter.

This is a hard platform limitation, re-confirmed during M5: the automation
account does not hold the GitHub App `workflows` permission, and pushing the
file is rejected outright:

```
! [remote rejected] refusing to allow a GitHub App to create or update
  workflow .github/workflows/ci.yml without `workflows` permission
```

A repository maintainer can activate it:

```bash
mkdir -p .github/workflows
cp ci/github-actions-ci.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml
git commit -m "ci: activate GitHub Actions pipeline"
git push
```

It requires no secrets.

## Fixed in M5

The pre-M5 version of this file invoked `npm run test:run --if-present`. That
script **does not exist** in `frontend/package.json` (the real one is `test`),
so `--if-present` made the frontend test step a silent no-op — it would have
reported success without running a single test, even once activated. The
workflow now calls `npm test`, and adds the `migrations` and `docker` jobs.

## Running the same checks locally

```bash
./scripts/ci-local.sh
```
