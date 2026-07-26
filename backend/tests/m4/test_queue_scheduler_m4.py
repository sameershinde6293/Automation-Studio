"""M4 tests: priority queue, worker pool scheduling and control primitives."""

from __future__ import annotations

import asyncio

import pytest

from app.domain.models.workflow import ExecutionPriority
from app.services.workflow.control import ControlHandle, ControlRegistry
from app.services.workflow.queue import (
    ExecutionQueue,
    QueueFullError,
    WorkerPool,
)
from app.services.workflow.runtime import (
    FieldSpec,
    NodeContext,
    NodeMetrics,
    NodeSchema,
    merge_metrics,
    truncate_output,
)

from .conftest import wait_for


# --------------------------------------------------------------------------- #
# Priority queue
# --------------------------------------------------------------------------- #
class TestExecutionQueue:
    def test_dequeues_in_priority_order(self):
        queue = ExecutionQueue()
        queue.put(1, priority=ExecutionPriority.LOW.value)
        queue.put(2, priority=ExecutionPriority.CRITICAL.value)
        queue.put(3, priority=ExecutionPriority.NORMAL.value)

        assert queue.get_nowait().execution_id == 2
        assert queue.get_nowait().execution_id == 3
        assert queue.get_nowait().execution_id == 1

    def test_fifo_within_a_priority_band(self):
        queue = ExecutionQueue()
        for eid in (10, 11, 12):
            queue.put(eid, priority=ExecutionPriority.NORMAL.value)
        assert [queue.get_nowait().execution_id for _ in range(3)] == [10, 11, 12]

    def test_empty_queue_returns_none(self):
        assert ExecutionQueue().get_nowait() is None

    def test_capacity_is_enforced(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "EXECUTION_QUEUE_MAX_SIZE", 2)
        queue = ExecutionQueue()
        queue.put(1)
        queue.put(2)
        with pytest.raises(QueueFullError) as excinfo:
            queue.put(3)
        assert excinfo.value.status_code == 429

    def test_enqueue_is_idempotent(self):
        queue = ExecutionQueue()
        queue.put(7)
        queue.put(7)
        assert queue.size() == 1

    def test_cancel_removes_a_queued_entry(self):
        queue = ExecutionQueue()
        queue.put(1)
        queue.put(2)
        assert queue.cancel(1) is True
        assert queue.size() == 1
        assert queue.get_nowait().execution_id == 2

    def test_cancel_unknown_returns_false(self):
        assert ExecutionQueue().cancel(999) is False

    def test_position_reflects_priority(self):
        queue = ExecutionQueue()
        queue.put(1, priority=ExecutionPriority.LOW.value)
        queue.put(2, priority=ExecutionPriority.HIGH.value)
        assert queue.position(2) == 1
        assert queue.position(1) == 2
        assert queue.position(404) is None

    def test_snapshot_exposes_waiting_entries(self):
        queue = ExecutionQueue()
        queue.put(5, priority=ExecutionPriority.HIGH.value, workflow_id=42)
        snapshot = queue.snapshot()
        assert snapshot[0]["execution_id"] == 5
        assert snapshot[0]["workflow_id"] == 42
        assert snapshot[0]["waiting_seconds"] >= 0

    def test_clear_empties_the_queue(self):
        queue = ExecutionQueue()
        queue.put(1)
        queue.clear()
        assert queue.size() == 0

    async def test_get_awaits_an_arrival(self):
        queue = ExecutionQueue()

        async def produce():
            await asyncio.sleep(0.05)
            queue.put(99)

        asyncio.create_task(produce())
        item = await queue.get(timeout=3)
        assert item is not None and item.execution_id == 99

    async def test_get_times_out_when_idle(self):
        queue = ExecutionQueue()
        assert await queue.get(timeout=0.1) is None


# --------------------------------------------------------------------------- #
# Worker pool
# --------------------------------------------------------------------------- #
class TestWorkerPool:
    async def test_workers_drain_the_queue(self):
        queue = ExecutionQueue()
        handled: list = []

        async def handler(execution_id: int) -> None:
            handled.append(execution_id)

        pool = WorkerPool(queue, handler, size=2)
        pool.start()
        try:
            for eid in range(5):
                queue.put(eid)
            await wait_for(lambda: len(handled) == 5, timeout=5)
        finally:
            await pool.shutdown(timeout=2)
        assert sorted(handled) == [0, 1, 2, 3, 4]

    async def test_concurrency_is_bounded_by_pool_size(self):
        queue = ExecutionQueue()
        active = 0
        peak = 0

        async def handler(execution_id: int) -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1

        pool = WorkerPool(queue, handler, size=2)
        pool.start()
        try:
            for eid in range(6):
                queue.put(eid)
            await wait_for(lambda: queue.size() == 0 and active == 0, timeout=5)
        finally:
            await pool.shutdown(timeout=2)
        assert peak <= 2

    async def test_handler_failure_does_not_kill_the_worker(self):
        queue = ExecutionQueue()
        seen: list = []

        async def handler(execution_id: int) -> None:
            seen.append(execution_id)
            if execution_id == 1:
                raise RuntimeError("boom")

        pool = WorkerPool(queue, handler, size=1)
        pool.start()
        try:
            queue.put(1)
            queue.put(2)
            await wait_for(lambda: 2 in seen, timeout=5)
        finally:
            stats = pool.stats()
            await pool.shutdown(timeout=2)
        assert stats["failed"] >= 1

    async def test_cancelled_queued_item_never_runs(self):
        queue = ExecutionQueue()
        handled: list = []

        async def handler(execution_id: int) -> None:
            handled.append(execution_id)

        queue.put(1)
        queue.put(2)
        queue.cancel(1)

        pool = WorkerPool(queue, handler, size=1)
        pool.start()
        try:
            await wait_for(lambda: 2 in handled, timeout=5)
        finally:
            await pool.shutdown(timeout=2)
        assert 1 not in handled

    async def test_shutdown_stops_the_pool(self):
        queue = ExecutionQueue()

        async def handler(execution_id: int) -> None:
            return None

        pool = WorkerPool(queue, handler, size=2)
        pool.start()
        assert pool.is_running
        await pool.shutdown(timeout=2)
        assert not pool.is_running


# --------------------------------------------------------------------------- #
# Control handles
# --------------------------------------------------------------------------- #
class TestControlHandle:
    async def test_pause_and_resume_transitions(self):
        handle = ControlHandle(execution_id=1)
        handle.bind_loop()

        assert handle.request_pause() is True
        assert handle.is_paused is True
        assert handle.request_pause() is False

        assert handle.request_resume() is True
        assert handle.is_paused is False
        assert handle.request_resume() is False

    async def test_wait_if_paused_unblocks_on_resume(self):
        handle = ControlHandle(execution_id=2)
        handle.bind_loop()
        handle.request_pause()

        waiter = asyncio.create_task(handle.wait_if_paused(0.05))
        await asyncio.sleep(0.1)
        assert not waiter.done()

        handle.request_resume()
        await asyncio.wait_for(waiter, timeout=3)

    async def test_wait_if_paused_returns_immediately_when_running(self):
        handle = ControlHandle(execution_id=3)
        handle.bind_loop()
        await asyncio.wait_for(handle.wait_if_paused(0.05), timeout=1)

    async def test_stop_unparks_a_paused_execution(self):
        handle = ControlHandle(execution_id=4)
        handle.bind_loop()
        handle.request_pause()
        assert handle.request_stop() is True
        assert handle.is_paused is False
        assert handle.should_halt is True
        await asyncio.wait_for(handle.wait_if_paused(0.05), timeout=1)

    async def test_cancel_sets_halt_and_stop_event(self):
        handle = ControlHandle(execution_id=5)
        handle.bind_loop()
        assert handle.request_cancel() is True
        assert handle.is_cancelled is True
        assert handle.should_halt is True
        assert handle.stop_event.is_set()
        assert handle.request_cancel() is False

    async def test_paused_duration_is_tracked(self):
        handle = ControlHandle(execution_id=6)
        handle.bind_loop()
        handle.request_pause()
        await asyncio.sleep(0.05)
        handle.request_resume()
        assert handle.total_paused_seconds >= 0.04

    async def test_pause_after_stop_is_refused(self):
        handle = ControlHandle(execution_id=7)
        handle.bind_loop()
        handle.request_stop()
        assert handle.request_pause() is False

    def test_registry_lifecycle(self):
        registry = ControlRegistry()
        handle = registry.create(11)
        assert registry.get(11) is handle
        assert registry.get_or_create(11) is handle
        assert registry.active_ids() == [11]
        registry.release(11)
        assert registry.get(11) is None
        registry.get_or_create(12)
        registry.clear()
        assert registry.active_ids() == []


# --------------------------------------------------------------------------- #
# Runtime primitives
# --------------------------------------------------------------------------- #
class TestRuntimePrimitives:
    def test_field_coercion_from_editor_strings(self):
        assert FieldSpec("n", "number").validate("2.5") == 2.5
        assert FieldSpec("i", "integer").validate("7") == 7
        assert FieldSpec("b", "boolean").validate("true") is True
        assert FieldSpec("b", "boolean").validate("no") is False
        assert FieldSpec("o", "object").validate('{"a":1}') == {"a": 1}
        assert FieldSpec("a", "array").validate("x, y") == ["x", "y"]
        assert FieldSpec("a", "array").validate("[1,2]") == [1, 2]

    def test_required_field_rejects_blank(self):
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError):
            FieldSpec("x", "string", required=True).validate("")

    def test_defaults_are_applied(self):
        assert FieldSpec("x", "string", default="fallback").validate(None) == "fallback"

    def test_enum_and_range_validation(self):
        from app.core.errors import ValidationError

        spec = FieldSpec("mode", "string", enum=["a", "b"])
        assert spec.validate("a") == "a"
        with pytest.raises(ValidationError):
            spec.validate("z")

        bounded = FieldSpec("n", "number", minimum=1, maximum=10)
        assert bounded.validate(5) == 5
        with pytest.raises(ValidationError):
            bounded.validate(99)

    def test_schema_preserves_unknown_keys(self):
        schema = NodeSchema(inputs=[FieldSpec("known", "string")])
        cleaned = schema.validate_inputs({"known": "v", "editor_only": 1})
        assert cleaned["editor_only"] == 1

    def test_node_context_variables_and_loops(self):
        context = NodeContext(execution_id=1, workflow_id=2, variables={"a": 1})
        assert context["vars"]["a"] == 1

        context.set_variable("b", 2)
        assert context.get_variable("b") == 2

        context.push_loop("item", 0, 3)
        assert context["loop"] == {"item": "item", "index": 0, "total": 3}
        context.pop_loop()
        assert context["loop"] == {}

    def test_node_context_records_outputs_under_id_and_name(self):
        context = NodeContext()
        context.record_output(5, "Fetch", {"ok": True})
        assert context[5] == {"ok": True}
        assert context["Fetch"] == {"ok": True}

    def test_node_context_never_shadows_reserved_keys(self):
        context = NodeContext(variables={"x": 1})
        context.record_output(9, "vars", {"malicious": True})
        assert context["vars"] == {"x": 1}

    def test_memory_is_bounded(self, monkeypatch):
        from app.infrastructure.config.settings import settings

        monkeypatch.setattr(settings, "AI_MEMORY_MAX_TURNS", 2)
        context = NodeContext()
        for i in range(10):
            context.remember("k", "user", f"m{i}")
        assert len(context.recall("k")) <= 4

    def test_truncate_output_caps_large_payloads(self):
        big = {"data": "x" * 5000}
        result = truncate_output(big, max_bytes=500)
        assert result["truncated"] is True
        assert result["size_bytes"] > 500

    def test_truncate_output_passes_small_payloads(self):
        assert truncate_output({"a": 1}, max_bytes=1000) == {"a": 1}

    def test_truncate_handles_unserialisable(self):
        result = truncate_output(object(), max_bytes=100)
        assert result["truncated"] is True

    def test_merge_metrics_accumulates(self):
        aggregate: dict = {}
        merge_metrics(aggregate, NodeMetrics(duration_ms=10, prompt_tokens=5,
                                             completion_tokens=5, total_tokens=10,
                                             cost_usd=0.01))
        merge_metrics(aggregate, NodeMetrics(duration_ms=20, total_tokens=4,
                                             cost_usd=0.02))
        assert aggregate["nodes_executed"] == 2
        assert aggregate["total_duration_ms"] == 30
        assert aggregate["total_tokens"] == 14
        assert round(aggregate["cost_usd"], 4) == 0.03


class TestExecutionPriority:
    def test_coerce_accepts_names_numbers_and_junk(self):
        assert ExecutionPriority.coerce("HIGH") is ExecutionPriority.HIGH
        assert ExecutionPriority.coerce(0) is ExecutionPriority.CRITICAL
        assert ExecutionPriority.coerce(None) is ExecutionPriority.NORMAL
        assert ExecutionPriority.coerce("nonsense") is ExecutionPriority.NORMAL
        # Nearest band wins.
        assert ExecutionPriority.coerce(11) is ExecutionPriority.HIGH
