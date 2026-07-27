# TODO

*Superseded. This file previously claimed "Version 1.0 is feature-complete" and
listed four future items that have all since been built (PostgreSQL support,
the visual node editor, desktop packaging, local LLM integration).*

Current work is tracked in:

- `PROJECT_STATUS.md` — milestone progress and readiness
- `KNOWN_ISSUES.md` — open defects and limitations
- `M7_RELEASE_AUDIT.md` — what is verified and what is not

## Highest-value remaining work

1. **Execute the Docker deployment path.** The only documented deployment path
   never run; the single biggest gap to a genuine ≥95% release candidate.
2. **Activate CI.** `ci/github-actions-ci.yml` needs a maintainer to copy it
   into `.github/workflows/`.
3. **Add a LICENSE file.** All rights are reserved by default without one.
4. **Lift the single-process constraint** — a shared queue and rate-limit store
   (Redis) before horizontal scaling is possible.
