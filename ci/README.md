# CI configuration

`github-actions-ci.yml` is the Creator OS continuous-integration pipeline:

| Job | What it runs |
| --- | --- |
| `backend` | `pytest` with coverage (1342 tests) |
| `migrations` | Alembic upgrade → downgrade → re-upgrade round trip, and a single-head check |
| `frontend` | `tsc --noEmit`, `vite build`, `vitest` (179 tests) |
| `docker` | Builds both production images |

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
