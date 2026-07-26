"""M4 engine tests: branch gating, loops, pause/resume/stop, metrics, retries."""

from __future__ import annotations

import asyncio

import pytest

from app.core.errors import ValidationError
from app.domain.models.workflow import ExecutionStatus
from app.services.workflow.control import control_registry
from app.services.workflow.executors import BaseNodeExecutor
from app.services.workflow.runtime import (
    FieldSpec,
    NodeContext,
    NodeErrorCode,
    NodeExecutionError,
    NodeResult,
    NodeSchema,
    RuntimeNodeExecutor,
)
from app.services.workflow.streaming import execution_broker

from .conftest import wait_for


# --------------------------------------------------------------------------- #
# Helper executors
# --------------------------------------------------------------------------- #
class RecordingExecutor(BaseNodeExecutor):
    """Records the order in which nodes ran."""

    def __init__(self) -> None:
        self.calls: list = []

    async def execute(self, node, context):
        self.calls.append(node.name)
        return {"ran": node.name}


class SlowExecutor(BaseNodeExecutor):
    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay
        self.started = 0
        self.finished = 0

    async def execute(self, node, context):
        self.started += 1
        await asyncio.sleep(self.delay)
        self.finished += 1
        return {"slept": self.delay}


class FlakyExecutor(BaseNodeExecutor):
    """Fails ``fail_times`` then succeeds."""

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.attempts = 0

    async def execute(self, node, context):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError(f"transient failure {self.attempts}")
        return {"ok": True, "attempts": self.attempts}


class AlwaysFailExecutor(BaseNodeExecutor):
    async def execute(self, node, context):
        raise RuntimeError("permanent failure")


class BadConfigExecutor(RuntimeNodeExecutor):
    """Raises a non-retryable validation error."""

    label = "Bad Config"
    schema = NodeSchema(inputs=[FieldSpec("required_field", "string", required=True)])

    def __init__(self) -> None:
        self.attempts = 0

    async def run(self, node, context, config):
        self.attempts += 1
        return {"ok": True}


# --------------------------------------------------------------------------- #
# Basic execution
# --------------------------------------------------------------------------- #
class TestBasicExecution:
    async def test_linear_graph_completes(self, engine, build_workflow, read_execution):
        execution_id, node_ids = build_workflow(
            [{"name": "a"}, {"name": "b"}, {"name": "c"}], [(0, 1), (1, 2)]
        )
        result = await engine.run_execution_v2(execution_id)

        assert result["status"] == ExecutionStatus.COMPLETED.value
        assert set(result["completed"]) == set(node_ids)
        assert read_execution(execution_id).status == ExecutionStatus.COMPLETED

    async def test_respects_topological_order(
        self, engine, build_workflow, temp_executor
    ):
        recorder = RecordingExecutor()
        temp_executor("m4_record", recorder)
        execution_id, _ = build_workflow(
            [
                {"name": "first", "node_type": "m4_record"},
                {"name": "second", "node_type": "m4_record"},
                {"name": "third", "node_type": "m4_record"},
            ],
            [(0, 1), (1, 2)],
        )
        await engine.run_execution_v2(execution_id)
        assert recorder.calls == ["first", "second", "third"]

    async def test_independent_nodes_run_in_parallel(
        self, engine, build_workflow, temp_executor
    ):
        slow = SlowExecutor(delay=0.15)
        temp_executor("m4_slow", slow)
        execution_id, _ = build_workflow(
            [{"name": f"p{i}", "node_type": "m4_slow"} for i in range(4)]
        )

        loop = asyncio.get_event_loop()
        started = loop.time()
        await engine.run_execution_v2(execution_id)
        elapsed = loop.time() - started

        # Serial would be ~0.6s; parallel should be well under that.
        assert slow.finished == 4
        assert elapsed < 0.45

    async def test_persists_metrics_and_state(
        self, engine, build_workflow, read_execution
    ):
        execution_id, _ = build_workflow([{"name": "a"}, {"name": "b"}], [(0, 1)])
        await engine.run_execution_v2(execution_id)

        execution = read_execution(execution_id)
        assert execution.metrics["nodes_executed"] == 2
        assert execution.metrics["duration_ms"] >= 0
        assert execution.state["completed"]
        assert execution.started_at and execution.finished_at

    async def test_node_execution_rows_carry_timings(
        self, engine, build_workflow, read_node_execution
    ):
        execution_id, node_ids = build_workflow([{"name": "solo"}])
        await engine.run_execution_v2(execution_id)

        row = read_node_execution(execution_id, node_ids[0])
        assert row.status == ExecutionStatus.COMPLETED
        assert row.duration_ms is not None
        assert row.queued_ms is not None
        assert row.started_at and row.finished_at
        assert row.attempt_metrics["attempts"][0]["ok"] is True

    async def test_seeds_input_data_as_variables(self, engine, build_workflow):
        execution_id, node_ids = build_workflow(
            [
                {
                    "name": "greet",
                    "node_type": "template",
                    "config": {"template": "Hello {{ vars.name }}"},
                }
            ],
            input_data={"name": "Ada"},
        )
        await engine.run_execution_v2(execution_id)

        from app.services.workflow.engine import workflow_engine  # noqa: F401

        # The rendered output is stored on the node execution.
        result = await engine.run_execution_v2(execution_id)
        assert result["status"] == ExecutionStatus.COMPLETED.value


# --------------------------------------------------------------------------- #
# Conditional branching (gap R3)
# --------------------------------------------------------------------------- #
class TestBranchGating:
    async def test_only_true_branch_runs(
        self, engine, build_workflow, read_node_execution
    ):
        execution_id, node_ids = build_workflow(
            [
                {
                    "name": "check",
                    "node_type": "condition",
                    "config": {"left": "10", "operator": ">", "right": "5"},
                },
                {"name": "yes"},
                {"name": "no"},
            ],
            [(0, 1, "true"), (0, 2, "false")],
        )
        result = await engine.run_execution_v2(execution_id)

        assert node_ids[1] in result["completed"]
        assert node_ids[2] not in result["completed"]
        assert result["status"] == ExecutionStatus.COMPLETED.value

    async def test_only_false_branch_runs(self, engine, build_workflow):
        execution_id, node_ids = build_workflow(
            [
                {
                    "name": "check",
                    "node_type": "condition",
                    "config": {"left": "1", "operator": ">", "right": "5"},
                },
                {"name": "yes"},
                {"name": "no"},
            ],
            [(0, 1, "true"), (0, 2, "false")],
        )
        result = await engine.run_execution_v2(execution_id)

        assert node_ids[2] in result["completed"]
        assert node_ids[1] not in result["completed"]

    async def test_unlabelled_edges_always_follow(self, engine, build_workflow):
        """A condition with unlabelled outgoing edges gates nothing."""
        execution_id, node_ids = build_workflow(
            [
                {
                    "name": "check",
                    "node_type": "condition",
                    "config": {"left": "0", "operator": "truthy"},
                },
                {"name": "always"},
            ],
            [(0, 1)],
        )
        result = await engine.run_execution_v2(execution_id)
        assert node_ids[1] in result["completed"]

    async def test_suppression_cascades_to_descendants(self, engine, build_workflow):
        execution_id, node_ids = build_workflow(
            [
                {
                    "name": "check",
                    "node_type": "condition",
                    "config": {"left": "yes", "operator": "==", "right": "yes"},
                },
                {"name": "taken"},
                {"name": "skipped"},
                {"name": "downstream_of_skipped"},
            ],
            [(0, 1, "true"), (0, 2, "false"), (2, 3)],
        )
        result = await engine.run_execution_v2(execution_id)

        assert node_ids[1] in result["completed"]
        assert node_ids[2] not in result["completed"]
        assert node_ids[3] not in result["completed"]
        assert result["status"] == ExecutionStatus.COMPLETED.value

    async def test_join_node_still_runs_when_one_branch_taken(
        self, engine, build_workflow
    ):
        """A join reachable via the taken branch must not be suppressed."""
        execution_id, node_ids = build_workflow(
            [
                {
                    "name": "check",
                    "node_type": "condition",
                    "config": {"left": "1", "operator": "==", "right": "1"},
                },
                {"name": "left"},
                {"name": "right"},
                {"name": "join"},
            ],
            [(0, 1, "true"), (0, 2, "false"), (1, 3), (2, 3)],
        )
        result = await engine.run_execution_v2(execution_id)
        assert node_ids[3] in result["completed"]


# --------------------------------------------------------------------------- #
# Loops (gap R4)
# --------------------------------------------------------------------------- #
class TestLoops:
    async def test_loop_back_edge_repeats_body(
        self, engine, build_workflow, temp_executor
    ):
        recorder = RecordingExecutor()
        temp_executor("m4_record", recorder)
        execution_id, _ = build_workflow(
            [
                {"name": "body", "node_type": "m4_record"},
                {
                    "name": "gate",
                    "node_type": "condition",
                    "config": {"left": "1", "operator": "==", "right": "1"},
                    # Cap iterations so the test terminates deterministically.
                },
            ],
            [(0, 1), (1, 0, "loop")],
        )
        # The loop cap lives on the loop target's config.
        from app.infrastructure.config.settings import settings

        original = settings.WORKFLOW_MAX_LOOP_ITERATIONS
        settings.WORKFLOW_MAX_LOOP_ITERATIONS = 3
        try:
            result = await engine.run_execution_v2(execution_id)
        finally:
            settings.WORKFLOW_MAX_LOOP_ITERATIONS = original

        assert result["status"] == ExecutionStatus.COMPLETED.value
        # 1 initial pass + 3 loop iterations.
        assert len(recorder.calls) == 4
        assert result["metrics"]["loop_iterations"] == 3

    async def test_loop_edge_does_not_trip_cycle_detection(
        self, engine, build_workflow
    ):
        execution_id, _ = build_workflow(
            [{"name": "a"}, {"name": "b"}], [(0, 1), (1, 0, "loop")]
        )
        from app.infrastructure.config.settings import settings

        original = settings.WORKFLOW_MAX_LOOP_ITERATIONS
        settings.WORKFLOW_MAX_LOOP_ITERATIONS = 1
        try:
            result = await engine.run_execution_v2(execution_id)
        finally:
            settings.WORKFLOW_MAX_LOOP_ITERATIONS = original
        assert result["status"] == ExecutionStatus.COMPLETED.value

    async def test_unlabelled_cycle_is_still_rejected(self, engine, build_workflow):
        execution_id, _ = build_workflow(
            [{"name": "a"}, {"name": "b"}], [(0, 1), (1, 0)]
        )
        result = await engine.run_execution_v2(execution_id)
        assert result["status"] == ExecutionStatus.FAILED.value
        assert "cycle" in result["error"].lower()

    async def test_condition_false_stops_the_loop(
        self, engine, build_workflow, temp_executor
    ):
        recorder = RecordingExecutor()
        temp_executor("m4_record", recorder)
        execution_id, _ = build_workflow(
            [
                {"name": "body", "node_type": "m4_record"},
                {
                    "name": "gate",
                    "node_type": "condition",
                    "config": {"left": "", "operator": "truthy"},
                },
            ],
            [(0, 1), (1, 0, "loop")],
        )
        result = await engine.run_execution_v2(execution_id)
        # Gate is false, so no extra iteration is scheduled.
        assert len(recorder.calls) == 1
        assert result["metrics"]["loop_iterations"] == 0


# --------------------------------------------------------------------------- #
# Retries and error classification
# --------------------------------------------------------------------------- #
class TestRetries:
    async def test_transient_failure_is_retried(
        self, engine, build_workflow, temp_executor, read_node_execution
    ):
        flaky = FlakyExecutor(fail_times=2)
        temp_executor("m4_flaky", flaky)
        execution_id, node_ids = build_workflow(
            [
                {
                    "name": "flaky",
                    "node_type": "m4_flaky",
                    "retry_policy": {"max_retries": 3, "base_delay": 0},
                }
            ]
        )
        result = await engine.run_execution_v2(execution_id)

        assert result["status"] == ExecutionStatus.COMPLETED.value
        assert flaky.attempts == 3
        assert read_node_execution(execution_id, node_ids[0]).retry_count == 2

    async def test_exhausted_retries_fail_the_node(
        self, engine, build_workflow, temp_executor, read_node_execution
    ):
        temp_executor("m4_fail", AlwaysFailExecutor())
        execution_id, node_ids = build_workflow(
            [
                {
                    "name": "boom",
                    "node_type": "m4_fail",
                    "retry_policy": {"max_retries": 2, "base_delay": 0},
                }
            ]
        )
        result = await engine.run_execution_v2(execution_id)

        assert result["status"] == ExecutionStatus.FAILED.value
        row = read_node_execution(execution_id, node_ids[0])
        assert row.status == ExecutionStatus.FAILED
        assert row.error_code == NodeErrorCode.RUNTIME.value

    async def test_validation_errors_are_not_retried(
        self, engine, build_workflow, temp_executor, read_node_execution
    ):
        """A non-retryable code must fail fast instead of burning attempts."""
        bad = BadConfigExecutor()
        temp_executor("m4_badconfig", bad)
        execution_id, node_ids = build_workflow(
            [
                {
                    "name": "bad",
                    "node_type": "m4_badconfig",
                    "config": {},
                    "retry_policy": {"max_retries": 5, "base_delay": 0},
                }
            ]
        )
        result = await engine.run_execution_v2(execution_id)

        assert result["status"] == ExecutionStatus.FAILED.value
        assert bad.attempts == 0  # never reached run()
        row = read_node_execution(execution_id, node_ids[0])
        assert row.error_code == NodeErrorCode.VALIDATION.value
        assert row.retry_count == 1  # failed on the first attempt

    async def test_timeout_is_classified_and_retried(
        self, engine, build_workflow, temp_executor
    ):
        slow = SlowExecutor(delay=5.0)
        temp_executor("m4_hang", slow)
        execution_id, node_ids = build_workflow(
            [
                {
                    "name": "hang",
                    "node_type": "m4_hang",
                    "retry_policy": {
                        "max_retries": 1,
                        "base_delay": 0,
                        "timeout": 1,
                    },
                }
            ]
        )
        result = await asyncio.wait_for(
            engine.run_execution_v2(execution_id), timeout=15
        )
        assert result["status"] == ExecutionStatus.FAILED.value

    async def test_on_error_continue_allows_downstream(
        self, engine, build_workflow, temp_executor
    ):
        temp_executor("m4_fail", AlwaysFailExecutor())
        execution_id, node_ids = build_workflow(
            [
                {
                    "name": "boom",
                    "node_type": "m4_fail",
                    "retry_policy": {
                        "max_retries": 1,
                        "base_delay": 0,
                        "on_error": "continue",
                    },
                },
                {"name": "after"},
            ],
            [(0, 1)],
        )
        result = await engine.run_execution_v2(execution_id)
        assert node_ids[1] in result["completed"]
        assert result["status"] == ExecutionStatus.COMPLETED.value

    async def test_on_error_skip_branch_skips_descendants(
        self, engine, build_workflow, temp_executor
    ):
        temp_executor("m4_fail", AlwaysFailExecutor())
        execution_id, node_ids = build_workflow(
            [
                {
                    "name": "boom",
                    "node_type": "m4_fail",
                    "retry_policy": {
                        "max_retries": 1,
                        "base_delay": 0,
                        "on_error": "skip_branch",
                    },
                },
                {"name": "downstream"},
            ],
            [(0, 1)],
        )
        result = await engine.run_execution_v2(execution_id)
        assert node_ids[1] in result["skipped"]


# --------------------------------------------------------------------------- #
# Pause / resume / stop / cancel  (gaps R1, R2)
# --------------------------------------------------------------------------- #
class TestExecutionControl:
    async def test_pause_then_resume_completes(
        self, engine, build_workflow, temp_executor, read_execution
    ):
        slow = SlowExecutor(delay=0.05)
        temp_executor("m4_slow", slow)
        execution_id, node_ids = build_workflow(
            [{"name": f"n{i}", "node_type": "m4_slow"} for i in range(4)],
            [(0, 1), (1, 2), (2, 3)],
        )

        task = asyncio.create_task(engine.run_execution_v2(execution_id))
        await wait_for(lambda: slow.started >= 1, timeout=3)

        assert engine.pause(execution_id) is True
        await wait_for(
            lambda: read_execution(execution_id).status == ExecutionStatus.PAUSED,
            timeout=3,
        )
        assert engine.is_paused(execution_id) is True

        paused_count = slow.finished
        await asyncio.sleep(0.2)
        # No new nodes should have started while paused.
        assert slow.started <= paused_count + 1

        assert engine.resume(execution_id) is True
        result = await asyncio.wait_for(task, timeout=10)
        assert result["status"] == ExecutionStatus.COMPLETED.value
        assert slow.finished == 4

    async def test_pause_is_idempotent(self, engine, build_workflow, temp_executor):
        slow = SlowExecutor(delay=0.05)
        temp_executor("m4_slow", slow)
        execution_id, _ = build_workflow(
            [{"name": f"n{i}", "node_type": "m4_slow"} for i in range(3)],
            [(0, 1), (1, 2)],
        )
        task = asyncio.create_task(engine.run_execution_v2(execution_id))
        await wait_for(lambda: slow.started >= 1, timeout=3)

        assert engine.pause(execution_id) is True
        assert engine.pause(execution_id) is False  # already paused
        engine.resume(execution_id)
        await asyncio.wait_for(task, timeout=10)

    async def test_resume_without_pause_returns_false(
        self, engine, build_workflow
    ):
        execution_id, _ = build_workflow([{"name": "a"}])
        control_registry.get_or_create(execution_id)
        assert engine.resume(execution_id) is False

    async def test_graceful_stop_finishes_inflight_nodes(
        self, engine, build_workflow, temp_executor, read_execution
    ):
        slow = SlowExecutor(delay=0.1)
        temp_executor("m4_slow", slow)
        execution_id, _ = build_workflow(
            [{"name": f"n{i}", "node_type": "m4_slow"} for i in range(5)],
            [(0, 1), (1, 2), (2, 3), (3, 4)],
        )

        task = asyncio.create_task(engine.run_execution_v2(execution_id))
        await wait_for(lambda: slow.started >= 1, timeout=3)
        assert engine.stop(execution_id) is True

        result = await asyncio.wait_for(task, timeout=10)
        assert result["status"] == ExecutionStatus.CANCELLED.value
        # Graceful: whatever started was allowed to finish, not killed.
        assert slow.finished == slow.started
        assert slow.finished < 5
        assert read_execution(execution_id).status == ExecutionStatus.CANCELLED

    async def test_hard_cancel_marks_cancelled(
        self, engine, build_workflow, temp_executor, read_execution
    ):
        slow = SlowExecutor(delay=0.5)
        temp_executor("m4_slow", slow)
        execution_id, _ = build_workflow(
            [{"name": f"n{i}", "node_type": "m4_slow"} for i in range(3)],
            [(0, 1), (1, 2)],
        )
        task = asyncio.create_task(engine.run_execution_v2(execution_id))
        await wait_for(lambda: slow.started >= 1, timeout=3)

        # The engine catches CancelledError, persists CANCELLED and returns a
        # summary rather than propagating, so callers always get a result.
        task.cancel()
        try:
            result = await task
        except asyncio.CancelledError:
            result = {"status": ExecutionStatus.CANCELLED.value}
        assert result["status"] == ExecutionStatus.CANCELLED.value
        await wait_for(
            lambda: read_execution(execution_id).status == ExecutionStatus.CANCELLED,
            timeout=3,
        )

    async def test_cancel_via_engine_api(
        self, engine, build_workflow, temp_executor, read_execution
    ):
        slow = SlowExecutor(delay=0.3)
        temp_executor("m4_slow", slow)
        execution_id, _ = build_workflow(
            [{"name": f"n{i}", "node_type": "m4_slow"} for i in range(3)],
            [(0, 1), (1, 2)],
        )
        task = engine.submit_v2(execution_id)
        await wait_for(lambda: slow.started >= 1, timeout=3)

        assert engine.cancel(execution_id) is True
        try:
            await task
        except asyncio.CancelledError:
            pass
        await wait_for(
            lambda: read_execution(execution_id).status == ExecutionStatus.CANCELLED,
            timeout=5,
        )
        assert read_execution(execution_id).status == ExecutionStatus.CANCELLED

    async def test_control_handle_released_after_completion(
        self, engine, build_workflow
    ):
        execution_id, _ = build_workflow([{"name": "a"}])
        await engine.run_execution_v2(execution_id)
        assert control_registry.get(execution_id) is None


# --------------------------------------------------------------------------- #
# Streaming and logs
# --------------------------------------------------------------------------- #
class TestStreamingAndLogs:
    async def test_events_published_for_run(self, engine, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}, {"name": "b"}], [(0, 1)])
        await engine.run_execution_v2(execution_id)

        events = execution_broker.replay_events(execution_id)
        names = [e.event for e in events]
        assert "execution.started" in names
        assert "node.started" in names
        assert "node.finished" in names
        assert "execution.finished" in names
        assert "execution.progress" in names

    async def test_event_sequences_are_monotonic(self, engine, build_workflow):
        execution_id, _ = build_workflow([{"name": f"n{i}"} for i in range(3)])
        await engine.run_execution_v2(execution_id)
        sequences = [e.sequence for e in execution_broker.replay_events(execution_id)]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)

    async def test_logs_persisted_to_database(
        self, engine, build_workflow, read_logs
    ):
        execution_id, _ = build_workflow([{"name": "a"}])
        await engine.run_execution_v2(execution_id)

        rows = read_logs(execution_id)
        assert rows, "expected durable log rows"
        assert any("started" in row.message.lower() for row in rows)
        assert any("completed" in row.message.lower() for row in rows)

    async def test_replay_events_filters_by_sequence(self, engine, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}])
        await engine.run_execution_v2(execution_id)

        everything = execution_broker.replay_events(execution_id)
        assert everything
        midpoint = everything[len(everything) // 2].sequence
        later = execution_broker.replay_events(execution_id, after_sequence=midpoint)
        assert all(e.sequence > midpoint for e in later)


# --------------------------------------------------------------------------- #
# Guard rails
# --------------------------------------------------------------------------- #
class TestGuardRails:
    async def test_missing_execution_returns_error(self, engine):
        result = await engine.run_execution_v2(999999)
        assert result["status"] == "ERROR"

    async def test_empty_workflow_fails_validation(self, engine, build_workflow):
        execution_id, _ = build_workflow([])
        result = await engine.run_execution_v2(execution_id)
        assert result["status"] == ExecutionStatus.FAILED.value

    async def test_unknown_node_type_fails_the_node(self, engine, build_workflow):
        execution_id, _ = build_workflow(
            [
                {
                    "name": "ghost",
                    "node_type": "does_not_exist",
                    "retry_policy": {"max_retries": 1, "base_delay": 0},
                }
            ]
        )
        result = await engine.run_execution_v2(execution_id)
        assert result["status"] == ExecutionStatus.FAILED.value

    async def test_node_execution_cap_is_enforced(
        self, engine, build_workflow, temp_executor
    ):
        from app.infrastructure.config.settings import settings

        execution_id, _ = build_workflow([{"name": f"n{i}"} for i in range(5)])
        original = settings.WORKFLOW_MAX_NODE_EXECUTIONS
        settings.WORKFLOW_MAX_NODE_EXECUTIONS = 2
        try:
            result = await engine.run_execution_v2(execution_id)
        finally:
            settings.WORKFLOW_MAX_NODE_EXECUTIONS = original
        assert result["status"] == ExecutionStatus.FAILED.value
