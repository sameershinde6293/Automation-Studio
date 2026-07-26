"""Workflow engine tests: DAG execution, retries, timeouts, cancellation,
error policies, cycle rejection and the V1.0 busy-wait regression.
"""

import asyncio

import pytest

from app.domain.models.workflow import ExecutionStatus
from app.domain.repositories.workflow_repository import (
    EdgeCreate,
    NodeCreate,
    WorkflowCreate,
    WorkflowExecutionCreate,
    edge_repo,
    node_execution_repo,
    node_repo,
    workflow_execution_repo,
    workflow_repo,
)
from app.infrastructure.config.settings import settings
from app.services.workflow.engine import (
    EVENT_EXECUTION_FINISHED,
    EVENT_EXECUTION_STARTED,
    EVENT_NODE_FINISHED,
    WorkflowEngine,
    _NodeSnapshot,
)
from app.services.workflow.executors import BaseNodeExecutor, executor_registry


@pytest.fixture
def engine(session_factory, monkeypatch):
    """Engine bound to an isolated in-memory database."""
    monkeypatch.setattr("app.services.workflow.engine.SessionLocal", session_factory)
    return WorkflowEngine()


@pytest.fixture
def graph_builder(session_factory):
    """Helper to build a workflow + execution and return the execution id."""

    def build(nodes, edges=(), workflow_name="WF"):
        db = session_factory()
        try:
            workflow = workflow_repo.create(db, WorkflowCreate(name=workflow_name))
            created = []
            for spec in nodes:
                created.append(
                    node_repo.create(
                        db,
                        NodeCreate(
                            workflow_id=workflow.id,
                            name=spec.get("name", "n"),
                            node_type=spec.get("node_type", "dummy"),
                            config=spec.get("config"),
                            retry_policy=spec.get("retry_policy"),
                        ),
                    )
                )
            for source_idx, target_idx in edges:
                edge_repo.create(
                    db,
                    EdgeCreate(
                        workflow_id=workflow.id,
                        source_id=created[source_idx].id,
                        target_id=created[target_idx].id,
                    ),
                )
            execution = workflow_execution_repo.create(
                db, WorkflowExecutionCreate(workflow_id=workflow.id)
            )
            return execution.id, [n.id for n in created]
        finally:
            db.close()

    return build


def read_execution(session_factory, execution_id):
    db = session_factory()
    try:
        return workflow_execution_repo.get(db, execution_id)
    finally:
        db.close()


def read_node_exec(session_factory, execution_id, node_id):
    db = session_factory()
    try:
        return node_execution_repo.get_by_execution_and_node(db, execution_id, node_id)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Custom executors used by the tests
# --------------------------------------------------------------------------- #
class AlwaysFailExecutor(BaseNodeExecutor):
    async def execute(self, node, context):
        raise RuntimeError("boom")


class FlakyExecutor(BaseNodeExecutor):
    """Fails ``fail_times`` times then succeeds."""

    def __init__(self, fail_times=1):
        self.fail_times = fail_times
        self.calls = 0

    async def execute(self, node, context):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"attempt {self.calls} failed")
        return {"result": "recovered", "attempts": self.calls}


class SlowExecutor(BaseNodeExecutor):
    def __init__(self, seconds=10):
        self.seconds = seconds

    async def execute(self, node, context):
        await asyncio.sleep(self.seconds)
        return {"result": "done"}


class ConcurrencyProbeExecutor(BaseNodeExecutor):
    def __init__(self):
        self.current = 0
        self.peak = 0

    async def execute(self, node, context):
        self.current += 1
        self.peak = max(self.peak, self.current)
        await asyncio.sleep(0.02)
        self.current -= 1
        return {"result": node.name}


class FalsyExecutor(BaseNodeExecutor):
    async def execute(self, node, context):
        return {}


@pytest.fixture
def register_executor():
    """Register temporary node types and clean them up afterwards."""
    registered = []

    def _register(node_type, executor):
        executor_registry.register(node_type, executor, override=True)
        registered.append(node_type)
        return executor

    yield _register
    for node_type in registered:
        executor_registry.unregister(node_type)


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestDagExecution:
    async def test_v1_dag_scenario_still_works(self, engine, graph_builder, session_factory):
        """Exact V1.0 regression scenario: fan-out of math_add nodes."""
        execution_id, node_ids = graph_builder(
            nodes=[
                {"name": "Start", "node_type": "math_add", "config": {"a": 10, "b": 5}},
                {"name": "Middle 1", "node_type": "math_add", "config": {"a": 1, "b": 2}},
                {"name": "Middle 2", "node_type": "math_add", "config": {"a": 1, "b": 3}},
            ],
            edges=[(0, 1), (0, 2)],
        )
        # Node ids are sequential from 1 in a fresh DB, matching V1.0 fixtures.
        await engine.run_execution(execution_id)

        execution = read_execution(session_factory, execution_id)
        assert execution.status == ExecutionStatus.COMPLETED

        n1 = read_node_exec(session_factory, execution_id, node_ids[0])
        n2 = read_node_exec(session_factory, execution_id, node_ids[1])
        n3 = read_node_exec(session_factory, execution_id, node_ids[2])
        assert n1.output_data["result"] == 15
        assert n2.output_data["result"] == 17
        assert n3.output_data["result"] == 18

    async def test_single_node(self, engine, graph_builder, session_factory):
        execution_id, _ = graph_builder([{"name": "solo", "node_type": "dummy"}])
        result = await engine.run_execution(execution_id)
        assert result["status"] == "COMPLETED"
        assert read_execution(session_factory, execution_id).status == ExecutionStatus.COMPLETED

    async def test_linear_chain_order(self, engine, graph_builder, session_factory):
        execution_id, node_ids = graph_builder(
            nodes=[{"name": f"n{i}", "node_type": "dummy"} for i in range(4)],
            edges=[(0, 1), (1, 2), (2, 3)],
        )
        await engine.run_execution(execution_id)
        for node_id in node_ids:
            assert read_node_exec(session_factory, execution_id, node_id).status == (
                ExecutionStatus.COMPLETED
            )

    async def test_context_available_downstream(self, engine, graph_builder, session_factory):
        execution_id, node_ids = graph_builder(
            nodes=[
                {"name": "src", "node_type": "math_add", "config": {"a": 20, "b": 22}},
                {"name": "dst", "node_type": "template",
                 "config": {"template": "value={{ src.result }}"}},
            ],
            edges=[(0, 1)],
        )
        await engine.run_execution(execution_id)
        dst = read_node_exec(session_factory, execution_id, node_ids[1])
        assert dst.output_data["result"] == "value=42"

    async def test_falsy_output_is_persisted(
        self, engine, graph_builder, session_factory, register_executor
    ):
        """V1.0 bug: ``if result:`` discarded empty-dict outputs."""
        register_executor("falsy", FalsyExecutor())
        execution_id, node_ids = graph_builder([{"name": "f", "node_type": "falsy"}])
        await engine.run_execution(execution_id)
        node_exec = read_node_exec(session_factory, execution_id, node_ids[0])
        assert node_exec.output_data == {}
        assert node_exec.status == ExecutionStatus.COMPLETED

    async def test_duration_recorded(self, engine, graph_builder, session_factory):
        execution_id, node_ids = graph_builder([{"name": "n", "node_type": "dummy"}])
        await engine.run_execution(execution_id)
        assert read_node_exec(session_factory, execution_id, node_ids[0]).duration_ms >= 0

    async def test_execution_state_checkpointed(self, engine, graph_builder, session_factory):
        execution_id, _ = graph_builder(
            nodes=[{"name": f"n{i}", "node_type": "dummy"} for i in range(3)],
            edges=[(0, 1), (1, 2)],
        )
        await engine.run_execution(execution_id)
        state = read_execution(session_factory, execution_id).state
        assert len(state["completed"]) == 3
        assert "duration_ms" in state


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestFailureHandling:
    async def test_failing_node_fails_workflow(
        self, engine, graph_builder, session_factory, register_executor
    ):
        register_executor("always_fail", AlwaysFailExecutor())
        execution_id, node_ids = graph_builder(
            [{"name": "bad", "node_type": "always_fail",
              "retry_policy": {"max_retries": 1, "base_delay": 0}}]
        )
        result = await engine.run_execution(execution_id)
        assert result["status"] == "FAILED"
        execution = read_execution(session_factory, execution_id)
        assert execution.status == ExecutionStatus.FAILED
        assert "boom" in execution.error

    async def test_retry_then_succeed(
        self, engine, graph_builder, session_factory, register_executor
    ):
        flaky = register_executor("flaky", FlakyExecutor(fail_times=2))
        execution_id, node_ids = graph_builder(
            [{"name": "flaky", "node_type": "flaky",
              "retry_policy": {"max_retries": 3, "base_delay": 0}}]
        )
        result = await engine.run_execution(execution_id)
        assert result["status"] == "COMPLETED"
        assert flaky.calls == 3
        node_exec = read_node_exec(session_factory, execution_id, node_ids[0])
        assert node_exec.output_data["result"] == "recovered"
        assert node_exec.retry_count == 2

    async def test_retries_exhausted(
        self, engine, graph_builder, session_factory, register_executor
    ):
        flaky = register_executor("flaky2", FlakyExecutor(fail_times=99))
        execution_id, _ = graph_builder(
            [{"name": "f", "node_type": "flaky2",
              "retry_policy": {"max_retries": 2, "base_delay": 0}}]
        )
        result = await engine.run_execution(execution_id)
        assert result["status"] == "FAILED"
        assert flaky.calls == 2

    async def test_on_error_continue(
        self, engine, graph_builder, session_factory, register_executor
    ):
        register_executor("always_fail2", AlwaysFailExecutor())
        execution_id, node_ids = graph_builder(
            nodes=[
                {"name": "bad", "node_type": "always_fail2",
                 "retry_policy": {"max_retries": 1, "base_delay": 0, "on_error": "continue"}},
                {"name": "after", "node_type": "dummy"},
            ],
            edges=[(0, 1)],
        )
        result = await engine.run_execution(execution_id)
        assert result["status"] == "COMPLETED"
        assert read_node_exec(session_factory, execution_id, node_ids[1]).status == (
            ExecutionStatus.COMPLETED
        )

    async def test_on_error_skip_branch(
        self, engine, graph_builder, session_factory, register_executor
    ):
        register_executor("always_fail3", AlwaysFailExecutor())
        execution_id, node_ids = graph_builder(
            nodes=[
                {"name": "bad", "node_type": "always_fail3",
                 "retry_policy": {"max_retries": 1, "base_delay": 0, "on_error": "skip_branch"}},
                {"name": "downstream", "node_type": "dummy"},
                {"name": "independent", "node_type": "dummy"},
            ],
            edges=[(0, 1)],
        )
        result = await engine.run_execution(execution_id)
        assert result["status"] == "FAILED"
        assert read_node_exec(session_factory, execution_id, node_ids[1]).status == (
            ExecutionStatus.SKIPPED
        )
        assert read_node_exec(session_factory, execution_id, node_ids[2]).status == (
            ExecutionStatus.COMPLETED
        )

    async def test_unknown_node_type_fails_gracefully(
        self, engine, graph_builder, session_factory
    ):
        execution_id, _ = graph_builder(
            [{"name": "x", "node_type": "nonexistent_type",
              "retry_policy": {"max_retries": 1, "base_delay": 0}}]
        )
        result = await engine.run_execution(execution_id)
        assert result["status"] == "FAILED"

    async def test_missing_execution_returns_error(self, engine):
        result = await engine.run_execution(999999)
        assert result["status"] == "ERROR"


# --------------------------------------------------------------------------- #
# Graph validation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestGraphValidation:
    async def test_cycle_rejected_with_clear_message(
        self, engine, graph_builder, session_factory
    ):
        execution_id, _ = graph_builder(
            nodes=[{"name": "a", "node_type": "dummy"}, {"name": "b", "node_type": "dummy"}],
            edges=[(0, 1), (1, 0)],
        )
        result = await engine.run_execution(execution_id)
        assert result["status"] == "FAILED"
        assert "Cycle detected" in result["error"]
        assert read_execution(session_factory, execution_id).status == ExecutionStatus.FAILED

    async def test_empty_workflow_rejected(self, engine, graph_builder):
        execution_id, _ = graph_builder(nodes=[])
        result = await engine.run_execution(execution_id)
        assert result["status"] == "FAILED"
        assert "no nodes" in result["error"]

    async def test_max_nodes_enforced(self, engine, graph_builder, monkeypatch):
        monkeypatch.setattr(settings, "WORKFLOW_MAX_NODES", 2)
        execution_id, _ = graph_builder(
            nodes=[{"name": f"n{i}", "node_type": "dummy"} for i in range(3)]
        )
        result = await engine.run_execution(execution_id)
        assert result["status"] == "FAILED"
        assert "maximum" in result["error"]


# --------------------------------------------------------------------------- #
# Timeouts, concurrency, cancellation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestTimeoutsAndConcurrency:
    async def test_node_timeout_enforced(
        self, engine, graph_builder, session_factory, register_executor
    ):
        register_executor("slow", SlowExecutor(seconds=10))
        execution_id, node_ids = graph_builder(
            [{"name": "slow", "node_type": "slow",
              "retry_policy": {"max_retries": 1, "base_delay": 0, "timeout": 0.1}}]
        )
        result = await asyncio.wait_for(engine.run_execution(execution_id), timeout=10)
        assert result["status"] == "FAILED"
        node_exec = read_node_exec(session_factory, execution_id, node_ids[0])
        assert "timed out" in node_exec.error

    async def test_concurrency_is_bounded(
        self, engine, graph_builder, register_executor, monkeypatch
    ):
        probe = register_executor("probe", ConcurrencyProbeExecutor())
        monkeypatch.setattr(settings, "WORKFLOW_MAX_PARALLEL_NODES", 2)
        execution_id, _ = graph_builder(
            nodes=[{"name": f"p{i}", "node_type": "probe"} for i in range(8)]
        )
        result = await engine.run_execution(execution_id)
        assert result["status"] == "COMPLETED"
        assert probe.peak <= 2

    async def test_parallel_nodes_actually_run_in_parallel(
        self, engine, graph_builder, register_executor, monkeypatch
    ):
        probe = register_executor("probe2", ConcurrencyProbeExecutor())
        monkeypatch.setattr(settings, "WORKFLOW_MAX_PARALLEL_NODES", 4)
        execution_id, _ = graph_builder(
            nodes=[{"name": f"p{i}", "node_type": "probe2"} for i in range(4)]
        )
        await engine.run_execution(execution_id)
        assert probe.peak > 1

    async def test_cancellation(
        self, engine, graph_builder, session_factory, register_executor
    ):
        register_executor("slow2", SlowExecutor(seconds=30))
        execution_id, _ = graph_builder([{"name": "s", "node_type": "slow2"}])

        task = asyncio.create_task(engine.run_execution(execution_id))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)
        assert read_execution(session_factory, execution_id).status == (
            ExecutionStatus.CANCELLED
        )

    async def test_no_busy_wait_when_branch_is_blocked(
        self, engine, graph_builder, register_executor
    ):
        """V1.0 regression: the scheduler spun at 100% CPU instead of settling."""
        register_executor("always_fail4", AlwaysFailExecutor())
        execution_id, _ = graph_builder(
            nodes=[
                {"name": "bad", "node_type": "always_fail4",
                 "retry_policy": {"max_retries": 1, "base_delay": 0, "on_error": "skip_branch"}},
                {"name": "b", "node_type": "dummy"},
                {"name": "c", "node_type": "dummy"},
            ],
            edges=[(0, 1), (1, 2)],
        )
        # Must terminate quickly rather than looping forever.
        result = await asyncio.wait_for(engine.run_execution(execution_id), timeout=10)
        assert result["status"] in {"FAILED", "COMPLETED"}


# --------------------------------------------------------------------------- #
# Task lifecycle & events
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestLifecycleAndEvents:
    async def test_submit_and_await(self, engine, graph_builder, session_factory):
        execution_id, _ = graph_builder([{"name": "n", "node_type": "dummy"}])
        task = engine.submit(execution_id)
        await task
        assert read_execution(session_factory, execution_id).status == (
            ExecutionStatus.COMPLETED
        )

    async def test_submit_twice_returns_same_task(
        self, engine, graph_builder, register_executor
    ):
        register_executor("slow3", SlowExecutor(seconds=1))
        execution_id, _ = graph_builder([{"name": "s", "node_type": "slow3"}])
        first = engine.submit(execution_id)
        second = engine.submit(execution_id)
        assert first is second
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass

    async def test_cancel_unknown_execution_returns_false(self, engine):
        assert engine.cancel(4242) is False

    async def test_is_running_reflects_state(
        self, engine, graph_builder, register_executor
    ):
        register_executor("slow4", SlowExecutor(seconds=1))
        execution_id, _ = graph_builder([{"name": "s", "node_type": "slow4"}])
        assert engine.is_running(execution_id) is False
        task = engine.submit(execution_id)
        await asyncio.sleep(0.05)
        assert engine.is_running(execution_id) is True
        engine.cancel(execution_id)
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_shutdown_cancels_active_tasks(
        self, engine, graph_builder, register_executor
    ):
        register_executor("slow5", SlowExecutor(seconds=30))
        execution_id, _ = graph_builder([{"name": "s", "node_type": "slow5"}])
        engine.submit(execution_id)
        await asyncio.sleep(0.05)
        await engine.shutdown(timeout=2)
        assert engine.active_tasks == {}

    async def test_events_published(self, engine, graph_builder):
        from app.infrastructure.events.event_bus import event_bus

        seen = []
        event_bus.subscribe(EVENT_EXECUTION_STARTED, lambda **kw: seen.append("started"))
        event_bus.subscribe(EVENT_NODE_FINISHED, lambda **kw: seen.append("node"))
        event_bus.subscribe(EVENT_EXECUTION_FINISHED, lambda **kw: seen.append("finished"))

        execution_id, _ = graph_builder([{"name": "n", "node_type": "dummy"}])
        await engine.run_execution(execution_id)
        assert seen == ["started", "node", "finished"]


# --------------------------------------------------------------------------- #
# Policy resolution & snapshots
# --------------------------------------------------------------------------- #
class TestPolicyResolution:
    def test_defaults_used_when_absent(self):
        node = _NodeSnapshot(1, "n", "dummy")
        policy = WorkflowEngine._policy(node)
        assert policy["max_retries"] == settings.WORKFLOW_MAX_RETRIES
        assert policy["on_error"] == "fail"

    def test_custom_policy_respected(self):
        node = _NodeSnapshot(
            1, "n", "dummy",
            retry_policy={"max_retries": 5, "base_delay": 2, "timeout": 10, "on_error": "continue"},
        )
        policy = WorkflowEngine._policy(node)
        assert policy == {
            "max_retries": 5, "base_delay": 2.0, "timeout": 10.0, "on_error": "continue"
        }

    def test_garbage_values_fall_back_to_defaults(self):
        node = _NodeSnapshot(
            1, "n", "dummy",
            retry_policy={"max_retries": "abc", "base_delay": None, "timeout": "x"},
        )
        policy = WorkflowEngine._policy(node)
        assert policy["max_retries"] == settings.WORKFLOW_MAX_RETRIES
        assert policy["timeout"] == settings.WORKFLOW_NODE_TIMEOUT_SECONDS

    def test_retries_clamped(self):
        node = _NodeSnapshot(1, "n", "dummy", retry_policy={"max_retries": 9999})
        assert WorkflowEngine._policy(node)["max_retries"] == 10

    def test_unknown_on_error_falls_back_to_fail(self):
        node = _NodeSnapshot(1, "n", "dummy", retry_policy={"on_error": "explode"})
        assert WorkflowEngine._policy(node)["on_error"] == "fail"

    def test_non_dict_policy_ignored(self):
        node = _NodeSnapshot(1, "n", "dummy", retry_policy="nope")
        assert WorkflowEngine._policy(node)["on_error"] == "fail"


class TestNodeSnapshot:
    def test_repr(self):
        assert "Node 1" in repr(_NodeSnapshot(1, "x", "dummy"))

    def test_from_orm_node(self, db):
        workflow = workflow_repo.create(db, WorkflowCreate(name="w"))
        node = node_repo.create(
            db,
            NodeCreate(
                workflow_id=workflow.id, name="n", node_type="dummy", config={"a": 1}
            ),
        )
        snapshot = _NodeSnapshot.from_orm_node(node)
        assert snapshot.id == node.id
        assert snapshot.config == {"a": 1}
        assert snapshot.node_type == "dummy"
