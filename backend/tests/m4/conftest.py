"""Shared fixtures for the M4 execution-engine suite."""

from __future__ import annotations

import asyncio

import pytest

from app.domain.repositories.workflow_repository import (
    EdgeCreate,
    NodeCreate,
    WorkflowCreate,
    WorkflowExecutionCreate,
    edge_repo,
    execution_log_repo,
    node_execution_repo,
    node_repo,
    workflow_execution_repo,
    workflow_repo,
)
from app.services.workflow.control import control_registry
from app.services.workflow.engine import WorkflowEngine
from app.services.workflow.queue import ExecutionQueue
from app.services.workflow.streaming import execution_broker


@pytest.fixture(autouse=True)
def isolate_runtime_state(session_factory, monkeypatch):
    """Point every engine-side SessionLocal at the test DB and reset globals.

    The broker and control registry are process-wide singletons; without this
    reset, state from one test leaks into the next.
    """
    for module in (
        "app.services.workflow.engine",
        "app.services.workflow.streaming",
        "app.services.ai.orchestrator",
        "app.services.media.pipeline",
        "app.infrastructure.database.database",
    ):
        try:
            monkeypatch.setattr(f"{module}.SessionLocal", session_factory)
        except AttributeError:
            pass

    execution_broker.reset()
    control_registry.clear()
    yield
    execution_broker.reset()
    control_registry.clear()


@pytest.fixture
def engine(session_factory, monkeypatch):
    """A WorkflowEngine bound to an isolated in-memory database and queue."""
    monkeypatch.setattr("app.services.workflow.engine.SessionLocal", session_factory)
    return WorkflowEngine(queue=ExecutionQueue())


@pytest.fixture
def api_client(session_factory, monkeypatch):
    """TestClient against the real app with the DB swapped for a temp one.

    Mirrors ``tests/api/conftest.py`` (fixtures are not shared across sibling
    packages). The lifespan is intentionally not run, so the execution worker
    pool stays down and ``enqueue`` falls back to direct submission.
    """
    from fastapi.testclient import TestClient

    from app.infrastructure.database.database import get_db

    for module in (
        "app.infrastructure.database.database",
        "app.services.workflow.engine",
        "app.services.workflow.streaming",
        "app.services.ai.orchestrator",
        "app.services.media.pipeline",
        "app.services.enterprise.auth",
    ):
        try:
            monkeypatch.setattr(f"{module}.SessionLocal", session_factory)
        except AttributeError:
            pass

    from app.main import create_app

    app = create_app()

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def build_workflow(session_factory):
    """Build a workflow graph and an execution; returns ids.

    ``nodes``  : list of dicts (name, node_type, config, retry_policy)
    ``edges``  : list of (source_index, target_index) or
                 (source_index, target_index, label)
    Returns ``(execution_id, [node_ids])``.
    """

    def build(nodes, edges=(), *, name="M4 WF", input_data=None, priority=50):
        db = session_factory()
        try:
            workflow = workflow_repo.create(db, WorkflowCreate(name=name))
            created = []
            for spec in nodes:
                created.append(
                    node_repo.create(
                        db,
                        NodeCreate(
                            workflow_id=workflow.id,
                            name=spec.get("name", f"n{len(created)}"),
                            node_type=spec.get("node_type", "dummy"),
                            config=spec.get("config"),
                            retry_policy=spec.get("retry_policy"),
                        ),
                    )
                )
            for edge in edges:
                source_idx, target_idx = edge[0], edge[1]
                label = edge[2] if len(edge) > 2 else None
                edge_repo.create(
                    db,
                    EdgeCreate(
                        workflow_id=workflow.id,
                        source_id=created[source_idx].id,
                        target_id=created[target_idx].id,
                        label=label,
                    ),
                )
            execution = workflow_execution_repo.create(
                db,
                WorkflowExecutionCreate(
                    workflow_id=workflow.id,
                    input_data=input_data,
                    priority=priority,
                ),
            )
            return execution.id, [n.id for n in created]
        finally:
            db.close()

    return build


@pytest.fixture
def read_execution(session_factory):
    def read(execution_id):
        db = session_factory()
        try:
            return workflow_execution_repo.get(db, execution_id)
        finally:
            db.close()

    return read


@pytest.fixture
def read_node_execution(session_factory):
    def read(execution_id, node_id):
        db = session_factory()
        try:
            return node_execution_repo.get_by_execution_and_node(
                db, execution_id, node_id
            )
        finally:
            db.close()

    return read


@pytest.fixture
def read_logs(session_factory):
    def read(execution_id, **kwargs):
        db = session_factory()
        try:
            return execution_log_repo.get_by_execution(db, execution_id, **kwargs)
        finally:
            db.close()

    return read


@pytest.fixture
def temp_executor():
    """Register an executor for the duration of one test, then remove it."""
    from app.services.workflow.executors import executor_registry

    registered = []

    def register(node_type, executor):
        executor_registry.register(node_type, executor, override=True)
        registered.append(node_type)
        return node_type

    yield register

    for node_type in registered:
        executor_registry.unregister(node_type)


async def wait_for(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll ``predicate`` until true or the timeout elapses."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()
