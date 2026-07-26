"""M4 tests: execution control/history/streaming API and the node library."""

from __future__ import annotations

import json

import pytest

from app.domain.models.workflow import ExecutionStatus
from app.domain.repositories.workflow_repository import workflow_execution_repo


@pytest.fixture
def workflow_with_graph(api_client):
    """Create a workflow with a small runnable graph; returns its id."""

    def build(nodes=None, edges=None, name="API WF"):
        response = api_client.post("/api/workflows/", json={"name": name})
        assert response.status_code == 200
        workflow_id = response.json()["id"]

        payload_nodes = nodes if nodes is not None else [
            {"id": 1, "name": "start", "node_type": "start"},
            {"id": 2, "name": "finish", "node_type": "end"},
        ]
        payload_edges = edges if edges is not None else [
            {"source_id": 1, "target_id": 2}
        ]
        saved = api_client.put(
            f"/api/workflows/{workflow_id}/graph",
            json={"nodes": payload_nodes, "edges": payload_edges},
        )
        assert saved.status_code == 200, saved.text
        return workflow_id, saved.json()["id_map"]

    return build


@pytest.fixture
def finished_execution(api_client, workflow_with_graph):
    """Run a workflow synchronously and return its execution id."""
    workflow_id, _ = workflow_with_graph()
    response = api_client.post(
        f"/api/workflows/{workflow_id}/executions", json={"wait": True}
    )
    assert response.status_code == 200, response.text
    return response.json()["execution_id"], workflow_id


# --------------------------------------------------------------------------- #
# Node library reachable from the editor
# --------------------------------------------------------------------------- #
class TestNodeLibraryAPI:
    def test_all_editor_node_types_are_registered(self, api_client):
        """The 22 palette types from M3 must all resolve (gap I1)."""
        editor_types = [
            "start", "end", "aiChat", "aiCompletion", "prompt", "variable",
            "condition", "loop", "delay", "httpRequest", "webhook", "python",
            "javascript", "database", "email", "file", "folder",
            "imageGeneration", "tts", "stt", "ffmpeg", "mediaProcessing",
        ]
        response = api_client.get("/api/system/node-types")
        assert response.status_code == 200
        available = {entry["type"] for entry in response.json()}
        missing = [t for t in editor_types if t not in available]
        assert missing == [], f"editor types missing from backend: {missing}"

    def test_node_schemas_expose_inputs_and_outputs(self, api_client):
        response = api_client.get("/api/system/node-schemas")
        assert response.status_code == 200
        entries = {e["type"]: e for e in response.json()}

        http_node = entries["httpRequest"]
        assert any(f["name"] == "url" and f["required"] for f in http_node["schema"]["inputs"])
        assert any(f["name"] == "status_code" for f in http_node["schema"]["outputs"])

    def test_schemas_hide_aliases_by_default(self, api_client):
        without = api_client.get("/api/system/node-schemas").json()
        with_aliases = api_client.get(
            "/api/system/node-schemas", params={"include_aliases": True}
        ).json()
        assert len(with_aliases) > len(without)

    def test_schemas_filter_by_category(self, api_client):
        response = api_client.get(
            "/api/system/node-schemas", params={"category": "media"}
        )
        assert response.status_code == 200
        assert all(e["category"] == "media" for e in response.json())

    def test_disabled_nodes_are_advertised_as_disabled(self, api_client):
        entries = {e["type"]: e for e in api_client.get("/api/system/node-schemas").json()}
        # python/javascript/database ship disabled by default.
        assert entries["python"]["enabled"] is False
        assert entries["javascript"]["enabled"] is False
        assert entries["database"]["enabled"] is False

    def test_editor_graph_saves_without_422(self, api_client, workflow_with_graph):
        """Before M4 saving any editor graph failed with 422 (gap I1)."""
        workflow_id, id_map = workflow_with_graph(
            nodes=[
                {"id": 1, "name": "Start", "node_type": "start"},
                {"id": 2, "name": "Ask", "node_type": "aiChat",
                 "config": {"prompt": "hi"}},
                {"id": 3, "name": "Done", "node_type": "end"},
            ],
            edges=[
                {"source_id": 1, "target_id": 2},
                {"source_id": 2, "target_id": 3},
            ],
        )
        assert len(id_map) == 3


# --------------------------------------------------------------------------- #
# Execution creation
# --------------------------------------------------------------------------- #
class TestExecutionCreation:
    def test_run_returns_stream_url(self, api_client, workflow_with_graph):
        workflow_id, _ = workflow_with_graph()
        response = api_client.post(
            f"/api/workflows/{workflow_id}/executions", json={"wait": False}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["stream_url"] == f"/api/executions/{body['execution_id']}/stream"

    def test_run_accepts_priority_and_inputs(self, api_client, workflow_with_graph):
        workflow_id, _ = workflow_with_graph()
        response = api_client.post(
            f"/api/workflows/{workflow_id}/executions",
            json={"wait": True, "priority": 0, "input_data": {"who": "world"}},
        )
        assert response.status_code == 200
        assert response.json()["status"] == ExecutionStatus.COMPLETED.value

    def test_invalid_priority_is_coerced(self, api_client, workflow_with_graph):
        workflow_id, _ = workflow_with_graph()
        response = api_client.post(
            f"/api/workflows/{workflow_id}/executions",
            json={"wait": False, "priority": 12345},
        )
        assert response.status_code == 200

    def test_validate_reports_loops_and_node_errors(
        self, api_client, workflow_with_graph
    ):
        workflow_id, _ = workflow_with_graph(
            nodes=[
                {"id": 1, "name": "A", "node_type": "start"},
                {"id": 2, "name": "B", "node_type": "end"},
            ],
            edges=[
                {"source_id": 1, "target_id": 2},
                {"source_id": 2, "target_id": 1, "label": "loop"},
            ],
        )
        response = api_client.post(f"/api/workflows/{workflow_id}/validate")
        assert response.status_code == 200
        body = response.json()
        assert body["is_valid"] is True
        assert len(body["loop_edges"]) == 1

    def test_validate_flags_bad_node_config(self, api_client, workflow_with_graph):
        workflow_id, _ = workflow_with_graph(
            nodes=[
                # httpRequest requires 'url'.
                {"id": 1, "name": "Fetch", "node_type": "httpRequest", "config": {}},
            ],
            edges=[],
        )
        response = api_client.post(f"/api/workflows/{workflow_id}/validate")
        body = response.json()
        assert body["is_valid"] is False
        assert body["node_errors"]
        assert "url" in json.dumps(body["node_errors"])


# --------------------------------------------------------------------------- #
# History API
# --------------------------------------------------------------------------- #
class TestHistoryAPI:
    def test_list_executions(self, api_client, finished_execution):
        execution_id, workflow_id = finished_execution
        response = api_client.get("/api/executions")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 1
        assert any(item["id"] == execution_id for item in body["items"])

    def test_filter_by_workflow_and_status(self, api_client, finished_execution):
        execution_id, workflow_id = finished_execution
        response = api_client.get(
            "/api/executions",
            params={"workflow_id": workflow_id, "status": "COMPLETED"},
        )
        assert response.status_code == 200
        assert any(item["id"] == execution_id for item in response.json()["items"])

    def test_unknown_status_is_rejected(self, api_client):
        response = api_client.get("/api/executions", params={"status": "BOGUS"})
        assert response.status_code == 422

    def test_pagination_bounds_are_enforced(self, api_client):
        assert api_client.get("/api/executions", params={"limit": 0}).status_code == 422
        assert api_client.get("/api/executions", params={"limit": 9999}).status_code == 422
        assert api_client.get("/api/executions", params={"skip": -1}).status_code == 422

    def test_detail_includes_nodes_and_flags(self, api_client, finished_execution):
        execution_id, _ = finished_execution
        response = api_client.get(f"/api/executions/{execution_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == execution_id
        assert "node_executions" in body
        assert body["is_running"] is False
        assert body["is_paused"] is False

    def test_detail_404_for_unknown(self, api_client):
        assert api_client.get("/api/executions/999999").status_code == 404

    def test_logs_endpoint(self, api_client, finished_execution):
        execution_id, _ = finished_execution
        response = api_client.get(f"/api/executions/{execution_id}/logs")
        assert response.status_code == 200
        body = response.json()
        assert body["execution_id"] == execution_id
        assert isinstance(body["items"], list)

    def test_logs_filtering_params(self, api_client, finished_execution):
        execution_id, _ = finished_execution
        response = api_client.get(
            f"/api/executions/{execution_id}/logs",
            params={"level": "INFO", "limit": 5, "after_sequence": 0},
        )
        assert response.status_code == 200
        assert all(item["level"] == "INFO" for item in response.json()["items"])

    def test_timeline_endpoint(self, api_client, finished_execution):
        execution_id, _ = finished_execution
        response = api_client.get(f"/api/executions/{execution_id}/timeline")
        assert response.status_code == 200
        body = response.json()
        assert body["execution_id"] == execution_id
        assert "entries" in body

    def test_stats_endpoint(self, api_client, finished_execution):
        response = api_client.get("/api/executions/stats")
        assert response.status_code == 200
        assert "by_status" in response.json()

    def test_queue_endpoint(self, api_client):
        response = api_client.get("/api/executions/queue")
        assert response.status_code == 200
        body = response.json()
        assert "queue_size" in body
        assert "workers" in body
        assert "streaming" in body

    def test_lineage_endpoint(self, api_client, finished_execution):
        execution_id, _ = finished_execution
        response = api_client.get(f"/api/executions/{execution_id}/lineage")
        assert response.status_code == 200
        assert response.json()["execution_id"] == execution_id


# --------------------------------------------------------------------------- #
# Control API
# --------------------------------------------------------------------------- #
class TestControlAPI:
    def test_pause_on_finished_execution_conflicts(
        self, api_client, finished_execution
    ):
        execution_id, _ = finished_execution
        response = api_client.post(f"/api/executions/{execution_id}/pause")
        assert response.status_code == 409

    def test_resume_on_finished_execution_conflicts(
        self, api_client, finished_execution
    ):
        execution_id, _ = finished_execution
        assert api_client.post(f"/api/executions/{execution_id}/resume").status_code == 409

    def test_stop_on_finished_execution_conflicts(
        self, api_client, finished_execution
    ):
        execution_id, _ = finished_execution
        assert api_client.post(f"/api/executions/{execution_id}/stop").status_code == 409

    def test_control_404_for_unknown_execution(self, api_client):
        for action in ("pause", "resume", "stop"):
            assert api_client.post(f"/api/executions/999999/{action}").status_code == 404

    def test_pause_on_pending_execution_reports_no_change(
        self, api_client, workflow_with_graph, session_factory
    ):
        workflow_id, _ = workflow_with_graph()
        created = api_client.post(
            f"/api/workflows/{workflow_id}/executions", json={"wait": False}
        ).json()
        execution_id = created["execution_id"]

        # Force a non-terminal status so the endpoint proceeds to the engine.
        db = session_factory()
        try:
            execution = workflow_execution_repo.get(db, execution_id)
            execution.status = ExecutionStatus.RUNNING
            db.commit()
        finally:
            db.close()

        response = api_client.post(f"/api/executions/{execution_id}/pause")
        assert response.status_code == 200
        assert "changed" in response.json()


# --------------------------------------------------------------------------- #
# Replay API
# --------------------------------------------------------------------------- #
class TestReplayAPI:
    def test_replay_creates_a_new_execution(self, api_client, finished_execution):
        execution_id, workflow_id = finished_execution
        response = api_client.post(
            f"/api/executions/{execution_id}/replay", json={"start": False}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["parent_execution_id"] == execution_id
        assert body["execution_id"] != execution_id
        assert body["replay_of"] == "replay"

    def test_replay_with_overrides(self, api_client, finished_execution):
        execution_id, _ = finished_execution
        response = api_client.post(
            f"/api/executions/{execution_id}/replay",
            json={"start": False, "priority": 10, "input_data": {"a": 1}},
        )
        assert response.status_code == 201

    def test_replay_404(self, api_client):
        assert api_client.post("/api/executions/999999/replay").status_code == 404

    def test_resume_failed_requires_failure(self, api_client, finished_execution):
        execution_id, _ = finished_execution
        response = api_client.post(
            f"/api/executions/{execution_id}/resume-failed", json={"start": False}
        )
        assert response.status_code == 409

    def test_resume_failed_on_failed_execution(
        self, api_client, finished_execution, session_factory
    ):
        execution_id, _ = finished_execution
        db = session_factory()
        try:
            execution = workflow_execution_repo.get(db, execution_id)
            execution.status = ExecutionStatus.FAILED
            db.commit()
        finally:
            db.close()

        response = api_client.post(
            f"/api/executions/{execution_id}/resume-failed", json={"start": False}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["replay_of"] == "resume_failed"
        assert body["parent_execution_id"] == execution_id


# --------------------------------------------------------------------------- #
# Streaming API
# --------------------------------------------------------------------------- #
class TestStreamingAPI:
    def test_events_polling_endpoint(self, api_client, finished_execution):
        execution_id, _ = finished_execution
        response = api_client.get(f"/api/executions/{execution_id}/events")
        assert response.status_code == 200
        body = response.json()
        assert body["execution_id"] == execution_id
        assert isinstance(body["events"], list)

    def test_events_404_for_unknown(self, api_client):
        assert api_client.get("/api/executions/999999/events").status_code == 404

    def test_stream_404_for_unknown(self, api_client):
        assert api_client.get("/api/executions/999999/stream").status_code == 404

    def test_stream_returns_sse_content_type(self, api_client, finished_execution):
        execution_id, _ = finished_execution
        with api_client.stream(
            "GET", f"/api/executions/{execution_id}/stream"
        ) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]

    def test_sse_frame_format(self):
        from app.services.workflow.streaming import ExecutionEvent, format_sse

        frame = format_sse(
            ExecutionEvent(execution_id=1, event="node.started", sequence=7,
                           payload={"node_id": 3})
        )
        assert frame.startswith("id: 7\n")
        assert "event: node.started\n" in frame
        assert frame.endswith("\n\n")
        data_line = [l for l in frame.splitlines() if l.startswith("data: ")][0]
        assert json.loads(data_line[6:])["node_id"] == 3


# --------------------------------------------------------------------------- #
# AI API additions
# --------------------------------------------------------------------------- #
class TestAIAPI:
    def test_estimate_from_text(self, api_client):
        response = api_client.post(
            "/api/ai/estimate", json={"text": "hello world", "model_name": "gpt-4o-mini"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["prompt_tokens"] > 0
        assert body["is_estimate"] is True

    def test_estimate_from_token_counts(self, api_client):
        response = api_client.post(
            "/api/ai/estimate",
            json={"prompt_tokens": 1000, "completion_tokens": 500,
                  "model_name": "gpt-4o"},
        )
        assert response.status_code == 200
        assert response.json()["cost_usd"] > 0

    def test_estimate_requires_input(self, api_client):
        assert api_client.post("/api/ai/estimate", json={}).status_code == 422

    def test_pricing_endpoint(self, api_client):
        response = api_client.get("/api/ai/pricing")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_traces_endpoint(self, api_client):
        response = api_client.get("/api/ai/traces")
        assert response.status_code == 200
        assert "items" in response.json()
        assert "stats" in response.json()

    def test_ai_health_endpoint(self, api_client):
        response = api_client.get("/api/ai/health")
        assert response.status_code == 200
        body = response.json()
        assert "providers" in body
        assert "circuits" in body
