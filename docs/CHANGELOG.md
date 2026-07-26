# Changelog

## [1.0.1-alpha] - 2026-07-26
### Changed
- Migrated FastAPI `on_event` startup/shutdown hooks to modern `asynccontextmanager` lifespans to prevent warnings and technical debt.
- Test coverage infrastructure instantiated leveraging `pytest-cov`.
- Added missing integration tests for REST API routers.

## [1.0.0] - 2026-07-26
### Added
- Workflow Engine (DAG execution)
- AI Runtime
- Media Pipeline
- Plugin Architecture
- Desktop UI
- Enterprise capabilities (Audit Logging, RBAC)
