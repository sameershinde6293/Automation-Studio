# CI configuration

`github-actions-ci.yml` is the Creator OS continuous-integration pipeline
(backend pytest + coverage gate, frontend typecheck + build + Vitest).

## Activating it

The automation account used for V1.1 development does not hold the GitHub App
`workflows` permission, so it cannot write into `.github/workflows/` directly.
A repository maintainer can activate the pipeline with:

```bash
mkdir -p .github/workflows
cp ci/github-actions-ci.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml
git commit -m "ci: activate GitHub Actions pipeline"
git push
```

The workflow is otherwise ready to run as-is and requires no secrets.

## Running the same checks locally

```bash
./scripts/ci-local.sh
```
