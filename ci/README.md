# CI configuration

`github-actions-ci.yml` is the Creator OS continuous-integration pipeline:

| Job | What it runs |
| --- | --- |
| `backend` | `pytest` with coverage (1529 tests) + docker asset validation + observability |
| `migrations` | Alembic upgrade → downgrade → re-upgrade round trip on SQLite, and a single-head check |
| `migrations-postgres` | **Added in M6.** The same round trip against a real PostgreSQL 16 service container, plus the M6 pool/regression suite |
| `frontend` | `tsc --noEmit`, `vite build`, `vitest` (179 tests) + artifact upload |
| `docker` | Builds both production images, inspects metadata (User, Healthcheck, ExposedPorts), validates compose config, smoke test `up --wait` |
| `examples` | **Added in M8.** Starts backend, runs `scripts/verify_examples.py` (4/4 workflows executed) |
| `production-build` | **Added in M8.** Validates production settings import + frontend production artifact |

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

## Activating it — M8 update

**Before M8:** The pipeline had never executed. The file lived in `ci/` rather than
`.github/workflows/`, and GitHub only runs workflows from the latter. M5 noted
that the automation account did not hold the GitHub App `workflows` permission,
and pushing the file was rejected outright:

```
! [remote rejected] refusing to allow a GitHub App to create or update
  workflow .github/workflows/ci.yml without `workflows` permission
```

**In M8:** We created `.github/workflows/ci.yml` (copy of `ci/github-actions-ci.yml` with M8 hardening) and attempted to push it.
If push succeeds, CI will run on every push/PR. If it is still rejected, a maintainer must:

```bash
mkdir -p .github/workflows
cp ci/github-actions-ci.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml
git commit -m "ci: activate GitHub Actions pipeline"
git push
```

It requires no secrets.

**Evidence of activation attempt:** See `docs/M8_VALIDATION_REPORT.md` §2.1 and `git log` for commit that adds `.github/workflows/ci.yml`. Check GitHub Actions tab for run.

## Fixed in M5

The pre-M5 version of this file invoked `npm run test:run --if-present`. That
script **does not exist** in `frontend/package.json` (the real one is `test`),
so `--if-present` made the frontend test step a silent no-op — it would have
reported success without running a single test, even once activated. The
workflow now calls `npm test`, and adds the `migrations` and `docker` jobs.

## Added in M8

- `docker` job expanded: inspects image User/Healthcheck/ExposedPorts, checks history, validates `docker compose config`, smoke test `up --wait` with curl health
- `examples` job: executes 4 example workflows against live backend (not just static)
- `production-build` job: validates production settings import + frontend dist artifact
- Backend job also runs `tests/m7/` and `tests/m8/` docker asset validation
- Frontend job uploads `dist` artifact
- All jobs produce artifacts (coverage.xml, dist)

## Running the same checks locally

```bash
./scripts/ci-local.sh
```

M8 extended version runs:

- Backend tests + coverage (1529 passed, 8 skipped)
- Migration round-trip
- M7+M8 docker asset validation (53 tests)
- Observability (logging, metrics, health)
- Docker static validation (44 checks)
- Production deployment check (source path)
- Frontend typecheck + build + tests
- Examples verification (4/4 with SSL_CERT_FILE workaround)
- Container runtime check (documents limitation if Docker absent)
