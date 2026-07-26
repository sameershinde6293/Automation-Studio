"""API surface tests covering V1.0 compatibility and the V1.1 additions."""

import pytest


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
class TestSystemEndpoints:
    def test_root(self, api_client):
        response = api_client.get("/")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_health_v1_shape_preserved(self, api_client):
        """V1.0 clients depend on this exact payload."""
        response = api_client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_liveness(self, api_client):
        body = api_client.get("/health/live").json()
        assert body["status"] == "healthy"
        assert body["uptime_seconds"] >= 0

    def test_readiness(self, api_client):
        body = api_client.get("/health/ready").json()
        assert body["status"] in {"ready", "degraded"}
        assert "database" in body["checks"]

    def test_system_info(self, api_client):
        body = api_client.get("/api/system/info").json()
        assert body["name"]
        assert body["version"]
        assert "features" in body

    def test_metrics(self, api_client):
        body = api_client.get("/api/system/metrics").json()
        assert body["uptime_seconds"] >= 0
        assert "pid" in body

    def test_node_type_catalog(self, api_client):
        catalog = api_client.get("/api/system/node-types").json()
        types = {entry["type"] for entry in catalog}
        assert {"dummy", "math_add", "http_request", "template", "branch"} <= types

    def test_events_endpoint(self, api_client):
        assert isinstance(api_client.get("/api/system/events").json(), list)

    def test_scheduler_jobs(self, api_client):
        assert isinstance(api_client.get("/api/system/scheduler/jobs").json(), list)

    def test_security_headers_applied(self, api_client):
        headers = api_client.get("/health").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Request-ID"]

    def test_openapi_available(self, api_client):
        assert api_client.get("/openapi.json").status_code == 200


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
class TestProjectEndpoints:
    def test_v1_create_and_list(self, api_client):
        response = api_client.post(
            "/api/projects/", json={"name": "API Project", "description": "API Test"}
        )
        assert response.status_code == 200
        assert response.json()["name"] == "API Project"

        listing = api_client.get("/api/projects/")
        assert listing.status_code == 200
        assert len(listing.json()) == 1

    def test_get_one(self, api_client):
        created = api_client.post("/api/projects/", json={"name": "P"}).json()
        fetched = api_client.get(f"/api/projects/{created['id']}").json()
        assert fetched["id"] == created["id"]

    def test_get_missing_returns_404_envelope(self, api_client):
        response = api_client.get("/api/projects/9999")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_update(self, api_client):
        created = api_client.post("/api/projects/", json={"name": "Old"}).json()
        updated = api_client.put(
            f"/api/projects/{created['id']}", json={"name": "New"}
        ).json()
        assert updated["name"] == "New"

    def test_update_missing(self, api_client):
        assert api_client.put("/api/projects/9999", json={"name": "x"}).status_code == 404

    def test_delete(self, api_client):
        created = api_client.post("/api/projects/", json={"name": "Gone"}).json()
        assert api_client.delete(f"/api/projects/{created['id']}").status_code == 204
        assert api_client.get(f"/api/projects/{created['id']}").status_code == 404

    def test_delete_missing(self, api_client):
        assert api_client.delete("/api/projects/9999").status_code == 404

    def test_pagination_bounds_enforced(self, api_client):
        assert api_client.get("/api/projects/?limit=99999").status_code == 422
        assert api_client.get("/api/projects/?skip=-1").status_code == 422

    def test_validation_error_envelope(self, api_client):
        response = api_client.post("/api/projects/", json={})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #
@pytest.fixture
def workflow(api_client):
    return api_client.post(
        "/api/workflows/", json={"name": "WF", "version": "1.0.0"}
    ).json()


class TestWorkflowCrud:
    def test_v1_create_and_list(self, api_client):
        response = api_client.post(
            "/api/workflows/", json={"name": "API Workflow", "version": "1.0.0"}
        )
        assert response.status_code == 200
        assert response.json()["name"] == "API Workflow"
        assert len(api_client.get("/api/workflows/").json()) == 1

    def test_get_one(self, api_client, workflow):
        assert api_client.get(f"/api/workflows/{workflow['id']}").json()["id"] == workflow["id"]

    def test_get_missing(self, api_client):
        assert api_client.get("/api/workflows/9999").status_code == 404

    def test_update(self, api_client, workflow):
        updated = api_client.put(
            f"/api/workflows/{workflow['id']}", json={"description": "desc"}
        ).json()
        assert updated["description"] == "desc"

    def test_delete(self, api_client, workflow):
        assert api_client.delete(f"/api/workflows/{workflow['id']}").status_code == 204

    def test_search(self, api_client):
        api_client.post("/api/workflows/", json={"name": "Alpha"})
        api_client.post("/api/workflows/", json={"name": "Beta"})
        results = api_client.get("/api/workflows/?search=alph").json()
        assert len(results) == 1
        assert results[0]["name"] == "Alpha"


class TestWorkflowNodes:
    def test_create_node(self, api_client, workflow):
        response = api_client.post(
            f"/api/workflows/{workflow['id']}/nodes",
            json={"name": "n1", "node_type": "dummy", "position_x": 10, "position_y": 20},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "n1"
        assert body["position_x"] == 10

    def test_create_node_unknown_type_rejected(self, api_client, workflow):
        response = api_client.post(
            f"/api/workflows/{workflow['id']}/nodes",
            json={"name": "n", "node_type": "not_a_real_type"},
        )
        assert response.status_code == 422
        assert "available" in response.json()["error"]["details"]

    def test_list_nodes(self, api_client, workflow):
        api_client.post(
            f"/api/workflows/{workflow['id']}/nodes",
            json={"name": "n", "node_type": "dummy"},
        )
        assert len(api_client.get(f"/api/workflows/{workflow['id']}/nodes").json()) == 1

    def test_update_node_position(self, api_client, workflow):
        node = api_client.post(
            f"/api/workflows/{workflow['id']}/nodes",
            json={"name": "n", "node_type": "dummy"},
        ).json()
        updated = api_client.put(
            f"/api/workflows/{workflow['id']}/nodes/{node['id']}",
            json={"position_x": 250.5, "position_y": 100},
        ).json()
        assert updated["position_x"] == 250.5

    def test_update_node_config(self, api_client, workflow):
        node = api_client.post(
            f"/api/workflows/{workflow['id']}/nodes",
            json={"name": "n", "node_type": "math_add"},
        ).json()
        updated = api_client.put(
            f"/api/workflows/{workflow['id']}/nodes/{node['id']}",
            json={"config": {"a": 1, "b": 2}},
        ).json()
        assert updated["config"] == {"a": 1, "b": 2}

    def test_update_node_wrong_workflow(self, api_client, workflow):
        other = api_client.post("/api/workflows/", json={"name": "Other"}).json()
        node = api_client.post(
            f"/api/workflows/{workflow['id']}/nodes",
            json={"name": "n", "node_type": "dummy"},
        ).json()
        response = api_client.put(
            f"/api/workflows/{other['id']}/nodes/{node['id']}", json={"name": "x"}
        )
        assert response.status_code == 404

    def test_delete_node_removes_its_edges(self, api_client, workflow):
        wid = workflow["id"]
        a = api_client.post(f"/api/workflows/{wid}/nodes", json={"name": "a", "node_type": "dummy"}).json()
        b = api_client.post(f"/api/workflows/{wid}/nodes", json={"name": "b", "node_type": "dummy"}).json()
        api_client.post(
            f"/api/workflows/{wid}/edges", json={"source_id": a["id"], "target_id": b["id"]}
        )
        assert api_client.delete(f"/api/workflows/{wid}/nodes/{a['id']}").status_code == 204
        assert api_client.get(f"/api/workflows/{wid}/edges").json() == []


class TestWorkflowEdges:
    @pytest.fixture
    def two_nodes(self, api_client, workflow):
        wid = workflow["id"]
        a = api_client.post(f"/api/workflows/{wid}/nodes", json={"name": "a", "node_type": "dummy"}).json()
        b = api_client.post(f"/api/workflows/{wid}/nodes", json={"name": "b", "node_type": "dummy"}).json()
        return wid, a, b

    def test_create_edge(self, api_client, two_nodes):
        wid, a, b = two_nodes
        response = api_client.post(
            f"/api/workflows/{wid}/edges", json={"source_id": a["id"], "target_id": b["id"]}
        )
        assert response.status_code == 200
        assert response.json()["source_id"] == a["id"]

    def test_self_edge_rejected(self, api_client, two_nodes):
        wid, a, _ = two_nodes
        response = api_client.post(
            f"/api/workflows/{wid}/edges", json={"source_id": a["id"], "target_id": a["id"]}
        )
        assert response.status_code == 422

    def test_duplicate_edge_rejected(self, api_client, two_nodes):
        wid, a, b = two_nodes
        payload = {"source_id": a["id"], "target_id": b["id"]}
        api_client.post(f"/api/workflows/{wid}/edges", json=payload)
        response = api_client.post(f"/api/workflows/{wid}/edges", json=payload)
        assert response.status_code == 409

    def test_cycle_creating_edge_rejected(self, api_client, two_nodes):
        wid, a, b = two_nodes
        api_client.post(
            f"/api/workflows/{wid}/edges", json={"source_id": a["id"], "target_id": b["id"]}
        )
        response = api_client.post(
            f"/api/workflows/{wid}/edges", json={"source_id": b["id"], "target_id": a["id"]}
        )
        assert response.status_code == 422
        assert "cycle" in response.json()["error"]["message"].lower()

    def test_edge_to_foreign_node_rejected(self, api_client, two_nodes):
        wid, a, _ = two_nodes
        response = api_client.post(
            f"/api/workflows/{wid}/edges", json={"source_id": a["id"], "target_id": 99999}
        )
        assert response.status_code == 422

    def test_delete_edge(self, api_client, two_nodes):
        wid, a, b = two_nodes
        edge = api_client.post(
            f"/api/workflows/{wid}/edges", json={"source_id": a["id"], "target_id": b["id"]}
        ).json()
        assert api_client.delete(f"/api/workflows/{wid}/edges/{edge['id']}").status_code == 204

    def test_delete_missing_edge(self, api_client, workflow):
        assert api_client.delete(f"/api/workflows/{workflow['id']}/edges/9999").status_code == 404


class TestWorkflowGraph:
    def test_get_empty_graph(self, api_client, workflow):
        graph = api_client.get(f"/api/workflows/{workflow['id']}/graph").json()
        assert graph["nodes"] == []
        assert graph["edges"] == []
        assert graph["workflow"]["id"] == workflow["id"]

    def test_replace_graph_creates_nodes_and_edges(self, api_client, workflow):
        wid = workflow["id"]
        payload = {
            "nodes": [
                {"id": -1, "name": "start", "node_type": "math_add",
                 "config": {"a": 1, "b": 2}, "position_x": 0, "position_y": 0},
                {"id": -2, "name": "end", "node_type": "dummy",
                 "position_x": 200, "position_y": 0},
            ],
            "edges": [{"source_id": -1, "target_id": -2}],
        }
        response = api_client.put(f"/api/workflows/{wid}/graph", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["node_count"] == 2
        assert body["edge_count"] == 1

        graph = api_client.get(f"/api/workflows/{wid}/graph").json()
        assert len(graph["nodes"]) == 2
        assert len(graph["edges"]) == 1
        assert graph["nodes"][0]["config"] == {"a": 1, "b": 2}

    def test_replace_graph_is_idempotent(self, api_client, workflow):
        wid = workflow["id"]
        payload = {"nodes": [{"id": -1, "name": "n", "node_type": "dummy"}], "edges": []}
        api_client.put(f"/api/workflows/{wid}/graph", json=payload)
        api_client.put(f"/api/workflows/{wid}/graph", json=payload)
        assert len(api_client.get(f"/api/workflows/{wid}/graph").json()["nodes"]) == 1

    def test_replace_graph_rejects_unknown_node_type(self, api_client, workflow):
        response = api_client.put(
            f"/api/workflows/{workflow['id']}/graph",
            json={"nodes": [{"id": -1, "name": "n", "node_type": "bogus"}], "edges": []},
        )
        assert response.status_code == 422

    def test_replace_graph_rejects_cycle(self, api_client, workflow):
        response = api_client.put(
            f"/api/workflows/{workflow['id']}/graph",
            json={
                "nodes": [
                    {"id": -1, "name": "a", "node_type": "dummy"},
                    {"id": -2, "name": "b", "node_type": "dummy"},
                ],
                "edges": [{"source_id": -1, "target_id": -2}, {"source_id": -2, "target_id": -1}],
            },
        )
        assert response.status_code == 422

    def test_replace_graph_clears_previous(self, api_client, workflow):
        wid = workflow["id"]
        api_client.put(
            f"/api/workflows/{wid}/graph",
            json={"nodes": [{"id": -1, "name": "old", "node_type": "dummy"}], "edges": []},
        )
        api_client.put(f"/api/workflows/{wid}/graph", json={"nodes": [], "edges": []})
        assert api_client.get(f"/api/workflows/{wid}/graph").json()["nodes"] == []


class TestWorkflowValidation:
    def test_valid_graph(self, api_client, workflow):
        wid = workflow["id"]
        api_client.put(
            f"/api/workflows/{wid}/graph",
            json={
                "nodes": [
                    {"id": -1, "name": "a", "node_type": "dummy"},
                    {"id": -2, "name": "b", "node_type": "dummy"},
                ],
                "edges": [{"source_id": -1, "target_id": -2}],
            },
        )
        body = api_client.post(f"/api/workflows/{wid}/validate").json()
        assert body["is_valid"] is True
        assert body["node_count"] == 2
        assert body["layers"] == [[1], [2]] or len(body["layers"]) == 2

    def test_empty_graph_is_invalid(self, api_client, workflow):
        body = api_client.post(f"/api/workflows/{workflow['id']}/validate").json()
        assert body["is_valid"] is False

    def test_validate_missing_workflow(self, api_client):
        assert api_client.post("/api/workflows/9999/validate").status_code == 404


class TestWorkflowExecutions:
    @pytest.fixture
    def runnable(self, api_client, workflow):
        wid = workflow["id"]
        api_client.put(
            f"/api/workflows/{wid}/graph",
            json={
                "nodes": [
                    {"id": -1, "name": "a", "node_type": "math_add", "config": {"a": 2, "b": 3}},
                ],
                "edges": [],
            },
        )
        return wid

    def test_run_synchronously(self, api_client, runnable):
        response = api_client.post(
            f"/api/workflows/{runnable}/executions", json={"wait": True}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "COMPLETED"

    def test_run_async_returns_pending(self, api_client, runnable):
        response = api_client.post(
            f"/api/workflows/{runnable}/executions", json={"wait": False}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "PENDING"

    def test_invalid_graph_rejected_before_execution(self, api_client, workflow):
        response = api_client.post(f"/api/workflows/{workflow['id']}/executions", json={})
        assert response.status_code == 422

    def test_get_execution_detail(self, api_client, runnable):
        execution_id = api_client.post(
            f"/api/workflows/{runnable}/executions", json={"wait": True}
        ).json()["execution_id"]
        body = api_client.get(f"/api/workflows/executions/{execution_id}").json()
        assert body["status"] == "COMPLETED"
        assert len(body["node_executions"]) == 1
        assert body["node_executions"][0]["output_data"]["result"] == 5

    def test_get_missing_execution(self, api_client):
        assert api_client.get("/api/workflows/executions/9999").status_code == 404

    def test_list_executions(self, api_client, runnable):
        api_client.post(f"/api/workflows/{runnable}/executions", json={"wait": True})
        listing = api_client.get(f"/api/workflows/{runnable}/executions").json()
        assert len(listing) == 1

    def test_cancel_finished_execution(self, api_client, runnable):
        execution_id = api_client.post(
            f"/api/workflows/{runnable}/executions", json={"wait": True}
        ).json()["execution_id"]
        body = api_client.post(f"/api/workflows/executions/{execution_id}/cancel").json()
        assert body["cancelled"] is False

    def test_cancel_missing_execution(self, api_client):
        assert api_client.post("/api/workflows/executions/9999/cancel").status_code == 404

    def test_v1_run_endpoint_preserved(self, api_client, runnable):
        """V1.0 clients POST /api/workflows/{execution_id}/run."""
        execution_id = api_client.post(
            f"/api/workflows/{runnable}/executions", json={"wait": False}
        ).json()["execution_id"]
        response = api_client.post(f"/api/workflows/{execution_id}/run")
        assert response.status_code == 200
        assert response.json()["status"] == "Execution submitted"


# --------------------------------------------------------------------------- #
# Plugins
# --------------------------------------------------------------------------- #
class TestPluginEndpoints:
    def test_register_and_list(self, api_client):
        response = api_client.post(
            "/api/plugins/", json={"name": "demo", "version": "1.0.0"}
        )
        assert response.status_code == 200
        assert len(api_client.get("/api/plugins/").json()) == 1

    def test_duplicate_name_conflicts(self, api_client):
        api_client.post("/api/plugins/", json={"name": "demo", "version": "1.0.0"})
        response = api_client.post("/api/plugins/", json={"name": "demo", "version": "2.0.0"})
        assert response.status_code == 409

    def test_get_one(self, api_client):
        created = api_client.post("/api/plugins/", json={"name": "d", "version": "1"}).json()
        assert api_client.get(f"/api/plugins/{created['id']}").json()["name"] == "d"

    def test_get_missing(self, api_client):
        assert api_client.get("/api/plugins/9999").status_code == 404

    def test_toggle(self, api_client):
        created = api_client.post("/api/plugins/", json={"name": "d", "version": "1"}).json()
        toggled = api_client.post(
            f"/api/plugins/{created['id']}/toggle", json={"is_active": True}
        ).json()
        assert toggled["is_active"] is True

    def test_toggle_missing(self, api_client):
        response = api_client.post("/api/plugins/9999/toggle", json={"is_active": True})
        assert response.status_code == 404

    def test_update(self, api_client):
        created = api_client.post("/api/plugins/", json={"name": "d", "version": "1"}).json()
        updated = api_client.put(
            f"/api/plugins/{created['id']}", json={"version": "2.0.0"}
        ).json()
        assert updated["version"] == "2.0.0"

    def test_delete(self, api_client):
        created = api_client.post("/api/plugins/", json={"name": "d", "version": "1"}).json()
        assert api_client.delete(f"/api/plugins/{created['id']}").status_code == 204

    def test_sdk_hooks_endpoint(self, api_client):
        assert isinstance(api_client.get("/api/plugins/sdk/hooks").json(), dict)

    def test_sdk_node_types_endpoint(self, api_client):
        assert isinstance(api_client.get("/api/plugins/sdk/node-types").json(), dict)


# --------------------------------------------------------------------------- #
# Enterprise
# --------------------------------------------------------------------------- #
class TestEnterpriseEndpoints:
    def test_list_roles(self, api_client):
        roles = api_client.get("/api/enterprise/roles").json()
        assert "admin" in roles
        assert "read" in roles["viewer"]

    def test_permission_allowed(self, api_client):
        body = api_client.post(
            "/api/enterprise/permissions/check", json={"role": "admin", "permission": "write"}
        ).json()
        assert body["allowed"] is True

    def test_permission_denied(self, api_client):
        body = api_client.post(
            "/api/enterprise/permissions/check", json={"role": "viewer", "permission": "write"}
        ).json()
        assert body["allowed"] is False

    def test_create_and_query_audit_events(self, api_client):
        api_client.post(
            "/api/enterprise/audit",
            json={"event_name": "test.event", "user_id": 1, "details": {"k": "v"}},
        )
        events = api_client.get("/api/enterprise/audit").json()
        assert len(events) == 1
        assert events[0]["event_name"] == "test.event"

    def test_audit_filter_by_name(self, api_client):
        api_client.post("/api/enterprise/audit", json={"event_name": "a"})
        api_client.post("/api/enterprise/audit", json={"event_name": "b"})
        assert len(api_client.get("/api/enterprise/audit?event_name=a").json()) == 1

    def test_audit_filter_by_user(self, api_client):
        api_client.post("/api/enterprise/audit", json={"event_name": "a", "user_id": 5})
        api_client.post("/api/enterprise/audit", json={"event_name": "b", "user_id": 6})
        assert len(api_client.get("/api/enterprise/audit?user_id=5").json()) == 1
