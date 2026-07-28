# TODO

*Superseded. This file previously claimed "Version 1.0 is feature-complete" and
listed four future items that have all since been built (PostgreSQL support,
the visual node editor, desktop packaging, local LLM integration).*

Current work is tracked in:

- `PROJECT_STATUS.md` — milestone progress and readiness
- `KNOWN_ISSUES.md` — open defects and limitations
- `M10_RELEASE_CERTIFICATION.md` — what is verified and what is not (current)
- `RELEASE_CHECKLIST.md` — release, install, deploy, ops and admin checklists

## Highest-value remaining work

1. **Execute the Docker deployment path.** Never run in M5–M10; the single
   biggest gap to a genuine ≥95% readiness score.
2. **Activate CI.** `ci/github-actions-ci.yml` needs a maintainer to copy it
   into `.github/workflows/`. It has **never executed** on any commit — the M8
   claim that it was activated was false and was corrected in M10.
3. **Add a LICENSE file.** All rights are reserved by default without one.
4. **Lift the single-process constraint** — a shared queue and rate-limit store
   (Redis) before horizontal scaling is possible.
