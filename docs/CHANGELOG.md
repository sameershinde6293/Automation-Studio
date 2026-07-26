# Changelog

## [0.3.0-alpha] - 2026-07-26
### Added
- Workflow models including Workflow, Node, Edge, WorkflowExecution, and NodeExecution.
- Alembic database migration to support workflow engine tables.
- Workflow engine supporting parallel node execution resolving DAG topological ordering.
- Node executor framework configured with simple test nodes.
- Retry policies and cancellation management built into execution engine logic.
