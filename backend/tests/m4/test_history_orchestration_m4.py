"""M4 tests: execution history, replay, resume-failed and AI orchestration."""

from __future__ import annotations

import asyncio

import pytest

from app.core.errors import ConflictError, NotFoundError, ProviderError
from app.domain.models.workflow import ExecutionStatus
from app.domain.repositories.workflow_repository import (
    NodeExecutionCreate,
    WorkflowExecutionCreate,
    node_execution_repo,
    workflow_execution_repo,
)
from app.services.ai.orchestration import (
    CircuitBreaker,
    CircuitState,
    CostModel,
    TraceRecorder,
)
from app.services.workflow.history import execution_history


# --------------------------------------------------------------------------- #
# History search and filtering
# --------------------------------------------------------------------------- #
class TestHistorySearch:
    def test_search_returns_paginated_results(self, db, build_workflow):
        for _ in range(3):
            build_workflow([{"name": "a"}])
        result = execution_history.search(db, limit=2)
        assert result["total"] >= 3
        assert len(result["items"]) == 2
        assert result["has_more"] is True

    def test_filter_by_workflow(self, db, build_workflow, session_factory):
        execution_id, _ = build_workflow([{"name": "a"}], name="Target")
        execution = workflow_execution_repo.get(db, execution_id)
        result = execution_history.search(db, workflow_id=execution.workflow_id)
        assert all(
            item["workflow_id"] == execution.workflow_id for item in result["items"]
        )

    def test_filter_by_status(self, db, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}])
        execution = workflow_execution_repo.get(db, execution_id)
        execution.status = ExecutionStatus.FAILED
        db.commit()

        failed = execution_history.search(db, statuses=["FAILED"])
        assert any(item["id"] == execution_id for item in failed["items"])

        completed = execution_history.search(db, statuses=["COMPLETED"])
        assert all(item["id"] != execution_id for item in completed["items"])

    def test_filter_by_trigger(self, db, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}])
        execution = workflow_execution_repo.get(db, execution_id)
        execution.trigger = "scheduled"
        db.commit()
        result = execution_history.search(db, trigger="scheduled")
        assert any(item["id"] == execution_id for item in result["items"])

    def test_search_matches_workflow_name(self, db, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}], name="Nightly Report")
        result = execution_history.search(db, search="Nightly")
        assert any(item["id"] == execution_id for item in result["items"])

    def test_search_matches_error_text(self, db, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}])
        execution = workflow_execution_repo.get(db, execution_id)
        execution.error = "connection refused by upstream"
        db.commit()
        result = execution_history.search(db, search="refused")
        assert any(item["id"] == execution_id for item in result["items"])

    def test_items_include_workflow_name(self, db, build_workflow):
        build_workflow([{"name": "a"}], name="Labelled WF")
        result = execution_history.search(db, limit=5)
        assert any(item.get("workflow_name") == "Labelled WF" for item in result["items"])

    def test_status_counts_aggregate(self, db, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}])
        execution = workflow_execution_repo.get(db, execution_id)
        execution.status = ExecutionStatus.COMPLETED
        db.commit()
        counts = workflow_execution_repo.status_counts(db)
        assert counts.get("COMPLETED", 0) >= 1

    def test_stats_reports_success_rate(self, db, build_workflow):
        ok_id, _ = build_workflow([{"name": "a"}])
        bad_id, _ = build_workflow([{"name": "b"}])
        workflow_execution_repo.get(db, ok_id).status = ExecutionStatus.COMPLETED
        workflow_execution_repo.get(db, bad_id).status = ExecutionStatus.FAILED
        db.commit()

        stats = execution_history.stats(db)
        assert stats["total"] >= 2
        assert 0.0 <= stats["success_rate"] <= 1.0


# --------------------------------------------------------------------------- #
# History detail / timeline / logs
# --------------------------------------------------------------------------- #
class TestHistoryDetail:
    async def test_detail_includes_node_executions(
        self, db, engine, build_workflow
    ):
        execution_id, node_ids = build_workflow([{"name": "a"}, {"name": "b"}], [(0, 1)])
        await engine.run_execution_v2(execution_id)

        detail = execution_history.get_detail(db, execution_id)
        assert detail["id"] == execution_id
        assert len(detail["node_executions"]) == 2
        assert detail["log_count"] > 0

    def test_detail_missing_execution_raises(self, db):
        with pytest.raises(NotFoundError):
            execution_history.get_detail(db, 987654)

    async def test_timeline_orders_and_summarises(self, db, engine, build_workflow):
        execution_id, _ = build_workflow(
            [{"name": "a"}, {"name": "b"}, {"name": "c"}], [(0, 1), (1, 2)]
        )
        await engine.run_execution_v2(execution_id)

        timeline = execution_history.get_timeline(db, execution_id)
        assert timeline["node_count"] == 3
        assert timeline["slowest_node"] is not None
        assert all("node_name" in entry for entry in timeline["entries"])

    async def test_logs_paginate_by_sequence(self, db, engine, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}])
        await engine.run_execution_v2(execution_id)

        first = execution_history.get_logs(db, execution_id, limit=1)
        assert first["count"] == 1
        after = execution_history.get_logs(
            db, execution_id, after_sequence=first["last_sequence"]
        )
        assert all(
            item["sequence"] > first["last_sequence"] for item in after["items"]
        )

    async def test_logs_filter_by_level(self, db, engine, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}])
        await engine.run_execution_v2(execution_id)
        info = execution_history.get_logs(db, execution_id, level="INFO")
        assert all(item["level"] == "INFO" for item in info["items"])


# --------------------------------------------------------------------------- #
# Replay and resume-failed
# --------------------------------------------------------------------------- #
class TestReplayAndResume:
    def test_replay_creates_linked_execution(self, db, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}], input_data={"k": "v"})
        replayed = execution_history.replay(db, execution_id)

        assert replayed.id != execution_id
        assert replayed.parent_execution_id == execution_id
        assert replayed.replay_of == "replay"
        assert replayed.input_data == {"k": "v"}
        assert replayed.trigger == f"replay:{execution_id}"

    def test_replay_accepts_new_inputs_and_priority(self, db, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}], input_data={"k": "v"})
        replayed = execution_history.replay(
            db, execution_id, priority=0, input_data={"k": "changed"}
        )
        assert replayed.input_data == {"k": "changed"}
        assert replayed.priority == 0

    def test_replay_missing_execution_raises(self, db):
        with pytest.raises(NotFoundError):
            execution_history.replay(db, 123456)

    async def test_replayed_execution_runs(self, db, engine, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}, {"name": "b"}], [(0, 1)])
        await engine.run_execution_v2(execution_id)

        replayed = execution_history.replay(db, execution_id)
        result = await engine.run_execution_v2(replayed.id)
        assert result["status"] == ExecutionStatus.COMPLETED.value

    def test_resume_failed_requires_failed_status(self, db, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}])
        execution = workflow_execution_repo.get(db, execution_id)
        execution.status = ExecutionStatus.COMPLETED
        db.commit()

        with pytest.raises(ConflictError):
            execution_history.resume_failed(db, execution_id)

    def test_resume_failed_seeds_completed_outputs(self, db, build_workflow):
        execution_id, node_ids = build_workflow([{"name": "a"}, {"name": "b"}], [(0, 1)])
        execution = workflow_execution_repo.get(db, execution_id)
        execution.status = ExecutionStatus.FAILED
        db.commit()

        ok = node_execution_repo.create(
            db, NodeExecutionCreate(execution_id=execution_id, node_id=node_ids[0])
        )
        ok.status = ExecutionStatus.COMPLETED
        ok.output_data = {"value": 42}
        bad = node_execution_repo.create(
            db, NodeExecutionCreate(execution_id=execution_id, node_id=node_ids[1])
        )
        bad.status = ExecutionStatus.FAILED
        db.commit()

        resumed = execution_history.resume_failed(db, execution_id)
        payload = resumed.input_data["__resume__"]
        assert payload["source_execution_id"] == execution_id
        assert payload["completed_outputs"][str(node_ids[0])] == {"value": 42}
        assert node_ids[1] in payload["failed_nodes"]
        assert resumed.replay_of == "resume_failed"

    def test_resume_allows_cancelled_executions(self, db, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}])
        execution = workflow_execution_repo.get(db, execution_id)
        execution.status = ExecutionStatus.CANCELLED
        db.commit()
        resumed = execution_history.resume_failed(db, execution_id)
        assert resumed.parent_execution_id == execution_id

    def test_lineage_reports_ancestors_and_children(self, db, build_workflow):
        execution_id, _ = build_workflow([{"name": "a"}])
        child = execution_history.replay(db, execution_id)
        grandchild = execution_history.replay(db, child.id)

        lineage = execution_history.lineage(db, grandchild.id)
        ancestor_ids = [item["id"] for item in lineage["ancestors"]]
        assert child.id in ancestor_ids
        assert execution_id in ancestor_ids

        parent_view = execution_history.lineage(db, execution_id)
        assert child.id in [item["id"] for item in parent_view["children"]]

    def test_lineage_tolerates_cycles(self, db, build_workflow):
        """A self-referencing parent must not spin forever."""
        execution_id, _ = build_workflow([{"name": "a"}])
        execution = workflow_execution_repo.get(db, execution_id)
        execution.parent_execution_id = execution_id
        db.commit()
        lineage = execution_history.lineage(db, execution_id)
        assert isinstance(lineage["ancestors"], list)


# --------------------------------------------------------------------------- #
# AI orchestration: fallback, breaker, cost, traces
# --------------------------------------------------------------------------- #
class _StubProvider:
    def __init__(self, content="ok", fail=False, usage=None):
        self.content = content
        self.fail = fail
        self.usage = usage or {}
        self.calls = 0

    async def generate(self, model_name, messages, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider unavailable")
        return {"content": self.content, "usage": self.usage}


class TestCircuitBreaker:
    def test_opens_after_threshold(self):
        breaker = CircuitBreaker(threshold=2, reset_seconds=60)
        assert breaker.is_available("p") is True
        breaker.record_failure("p", "err")
        assert breaker.is_available("p") is True
        breaker.record_failure("p", "err")
        assert breaker.is_available("p") is False
        assert breaker.state("p") == CircuitState.OPEN

    def test_success_closes_the_circuit(self):
        breaker = CircuitBreaker(threshold=1, reset_seconds=60)
        breaker.record_failure("p", "err")
        assert breaker.is_available("p") is False
        breaker.record_success("p")
        assert breaker.is_available("p") is True
        assert breaker.state("p") == CircuitState.CLOSED

    def test_half_open_after_cooldown(self):
        breaker = CircuitBreaker(threshold=1, reset_seconds=0.1)
        breaker.record_failure("p", "err")
        assert breaker.is_available("p") is False
        import time

        time.sleep(0.15)
        assert breaker.is_available("p") is True
        assert breaker.state("p") == CircuitState.HALF_OPEN

    def test_snapshot_and_reset(self):
        breaker = CircuitBreaker(threshold=1, reset_seconds=60)
        breaker.record_failure("p", "boom")
        snapshot = breaker.snapshot()
        assert snapshot["p"]["state"] == CircuitState.OPEN
        assert snapshot["p"]["last_error"] == "boom"
        breaker.reset()
        assert breaker.snapshot() == {}


class TestCostModel:
    def test_known_model_pricing(self):
        model = CostModel()
        estimate = model.estimate("gpt-4o-mini", 1000, 1000)
        assert estimate["cost_usd"] > 0
        assert estimate["is_estimate"] is True

    def test_prefix_match_inherits_pricing(self):
        model = CostModel()
        base = model.pricing_for("gpt-4o")
        variant = model.pricing_for("gpt-4o-2024-08-06")
        assert variant.prompt_per_1k == base.prompt_per_1k

    def test_override_registration(self):
        model = CostModel()
        model.register("custom", 1.0, 2.0)
        estimate = model.estimate("custom", 1000, 1000)
        assert estimate["cost_usd"] == pytest.approx(3.0)

    def test_unknown_model_defaults_to_zero(self):
        assert CostModel().estimate("mystery", 1000, 1000)["cost_usd"] == 0.0


class TestTraceRecorder:
    def test_records_and_reports_stats(self):
        recorder = TraceRecorder(size=10)
        ok = recorder.start("chat")
        ok.success = True
        ok.total_tokens = 10
        ok.cost_usd = 0.5
        recorder.finish(ok)

        bad = recorder.start("chat")
        bad.success = False
        recorder.finish(bad)

        stats = recorder.stats()
        assert stats["count"] == 2
        assert stats["success_rate"] == 0.5
        assert len(recorder.recent(only_failures=True)) == 1

    def test_buffer_is_bounded(self):
        recorder = TraceRecorder(size=3)
        for _ in range(10):
            recorder.finish(recorder.start("x"))
        assert recorder.stats()["count"] == 3

    def test_empty_stats(self):
        assert TraceRecorder(size=5).stats()["count"] == 0


class TestOrchestratorGenerate:
    @pytest.fixture
    def orchestrator(self, monkeypatch, session_factory):
        from app.services.ai.orchestrator import AIOrchestrator

        monkeypatch.setattr(
            "app.services.ai.orchestrator.SessionLocal", session_factory
        )
        instance = AIOrchestrator()
        return instance

    async def test_generate_uses_first_healthy_provider(self, orchestrator):
        primary = _StubProvider(content="from-primary")
        orchestrator.providers = {"mock": primary}
        result = await orchestrator.generate(
            [{"role": "user", "content": "hi"}], provider="mock"
        )
        assert result["content"] == "from-primary"
        assert result["fallback_used"] is False
        assert result["usage"]["total_tokens"] > 0

    async def test_falls_back_when_primary_fails(self, orchestrator, monkeypatch):
        from app.infrastructure.config.settings import settings

        broken = _StubProvider(fail=True)
        backup = _StubProvider(content="from-backup")
        orchestrator.providers = {"broken": broken, "mock": backup}
        monkeypatch.setattr(settings, "AI_FALLBACK_CHAIN", ["broken", "mock"])

        result = await orchestrator.generate([{"role": "user", "content": "hi"}])
        assert result["content"] == "from-backup"
        assert result["fallback_used"] is True
        assert broken.calls == 1

    async def test_raises_when_every_provider_fails(self, orchestrator, monkeypatch):
        from app.infrastructure.config.settings import settings

        orchestrator.providers = {"a": _StubProvider(fail=True),
                                  "b": _StubProvider(fail=True)}
        monkeypatch.setattr(settings, "AI_FALLBACK_CHAIN", ["a", "b"])
        with pytest.raises(ProviderError):
            await orchestrator.generate([{"role": "user", "content": "hi"}])

    async def test_failures_trip_the_circuit(self, orchestrator, monkeypatch):
        from app.infrastructure.config.settings import settings

        orchestrator.providers = {"flaky": _StubProvider(fail=True)}
        monkeypatch.setattr(settings, "AI_FALLBACK_CHAIN", ["flaky"])
        monkeypatch.setattr(settings, "AI_CIRCUIT_BREAKER_THRESHOLD", 1)

        with pytest.raises(ProviderError):
            await orchestrator.generate([{"role": "user", "content": "x"}])
        assert orchestrator.circuit_breaker.state("flaky") == CircuitState.OPEN

    async def test_disabling_fallback_pins_the_provider(self, orchestrator):
        orchestrator.providers = {
            "only": _StubProvider(fail=True),
            "mock": _StubProvider(content="unused"),
        }
        with pytest.raises(ProviderError):
            await orchestrator.generate(
                [{"role": "user", "content": "x"}],
                provider="only",
                allow_fallback=False,
            )

    async def test_empty_messages_rejected(self, orchestrator):
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError):
            await orchestrator.generate([])

    async def test_trace_is_recorded(self, orchestrator):
        orchestrator.providers = {"mock": _StubProvider(content="hello")}
        await orchestrator.generate(
            [{"role": "user", "content": "hi"}], provider="mock"
        )
        assert orchestrator.traces.stats()["count"] == 1

    def test_estimate_cost(self, orchestrator):
        estimate = orchestrator.estimate_cost("word " * 100, model_name="gpt-4o-mini")
        assert estimate["prompt_tokens"] > 0
        assert estimate["cost_usd"] >= 0

    def test_optional_providers_absent_by_default(self, orchestrator):
        assert orchestrator.get_image_provider() is None
        assert orchestrator.get_speech_provider("tts") is None
        assert orchestrator.get_speech_provider("stt") is None

    def test_optional_provider_registration(self, orchestrator):
        sentinel = object()
        orchestrator.register_image_provider("fake", sentinel)
        orchestrator.register_speech_provider("tts", "fake", sentinel)
        assert orchestrator.get_image_provider() is sentinel
        assert orchestrator.get_speech_provider("tts", "fake") is sentinel
        orchestrator.clear_optional_providers()
        assert orchestrator.get_image_provider() is None

    def test_invalid_speech_kind_rejected(self, orchestrator):
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError):
            orchestrator.register_speech_provider("bogus", "x", object())

    def test_health_reports_state(self, orchestrator):
        health = orchestrator.health()
        assert "providers" in health
        assert "circuits" in health
        assert "fallback_chain" in health

    def test_render_prompt_interpolates(self):
        from app.services.ai.orchestrator import render_prompt

        assert render_prompt("Hi {{ name }}", {"name": "Ada"}) == "Hi Ada"
