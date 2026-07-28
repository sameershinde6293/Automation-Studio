# Roadmap & Progress

## V1.0 foundation

- [x] 1. Read all documentation.
- [x] 2. Build dependency graph.
- [x] 3. Produce implementation plan.
- [x] 4. Bootstrap repository.
- [x] 5. Implement infrastructure.
- [x] 6. Implement database.
- [x] 7. Implement workflow engine.
- [x] 8. Implement AI runtime.
- [x] 9. Implement media pipeline.
- [x] 10. Implement desktop UI.
- [x] 11. Implement plugin SDK.
- [x] 12. Implement enterprise features.
- [x] 13. Implement AI automation.

## V1.1 milestones

- [x] M0 — Repair & hygiene
- [x] M1 — Backend core hardening
- [x] M2 — API expansion & service completion
- [x] M3 — Drag-and-drop workflow editor
- [x] M4 — Execution engine & AI orchestration
- [x] M5 — Production readiness & platform hardening
- [x] M6 — Production validation & scalability
- [x] M7 — Production deployment & Release Candidate (**88%**, `M7_RELEASE_AUDIT.md`)
- [x] M8 — Infrastructure validation & container assets (**92%**, `M8_VALIDATION_REPORT.md`)
- [x] M9 — Production staging & real-world validation (**94%**, `M9_VALIDATION_REPORT.md`)
- [x] **M10 — v1.1.0 release & final production certification** (**94%**, `M10_RELEASE_CERTIFICATION.md`)

> Note: items 14 ("harden for production") and 15 ("produce release candidate")
> were both ticked in the V1.0 list while neither was true — the deployment path
> had never been executed. M7 is the milestone that actually did the work, and
> it is honest about what remains unverified (Docker).

## Planned

- [ ] M11 — Durable queue & horizontal scaling (Redis)
- [ ] M12 — Media pipeline UX & first-party AI providers
- [ ] Verify the Docker deployment path on a machine with a container runtime
- [ ] Activate CI — never executed on any commit to date
- [ ] Add a LICENSE
- [ ] Run a 24-hour soak and a multi-replica trial

> The M8/M9 entries above were previously listed as *planned* under the wrong
> numbers while milestones of the same names had already shipped. Corrected in
> the M10 self-audit.
