#!/usr/bin/env python3
"""End-to-end smoke test for the M4 execution engine.

Exercises the full path against a **running** server: save an editor-shaped
graph, validate it, run it, confirm conditional branch gating actually skipped
the untaken branch, then read back history, logs, timeline, replay and the SSE
stream.

Usage:
    cd backend
    DATABASE_URL="sqlite:///./e2e.db" .venv/bin/python -m alembic upgrade head
    DATABASE_URL="sqlite:///./e2e.db" RATE_LIMIT_ENABLED=false \
        .venv/bin/python -m uvicorn app.main:app --port 8113 &
    python3 ../scripts/e2e_execution_smoke.py

Exits non-zero on the first failed assertion.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("CREATOR_OS_URL", "http://127.0.0.1:8113")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    print("health:", call("GET", "/health")[1])

    _, workflow = call("POST", "/api/workflows/", {"name": "E2E Branch+Loop"})
    workflow_id = workflow["id"]

    # start -> condition -> (true) delay -> end ; (false) variable
    nodes = [
        {"id": 1, "name": "Start", "node_type": "start",
         "config": {"variables": {"threshold": 5}}},
        {"id": 2, "name": "Check", "node_type": "condition",
         "config": {"left": "10", "operator": ">", "right": "{{ vars.threshold }}"}},
        {"id": 3, "name": "HotPath", "node_type": "delay", "config": {"seconds": 0.05}},
        {"id": 4, "name": "ColdPath", "node_type": "variable",
         "config": {"name": "cold", "value": "never"}},
        {"id": 5, "name": "Done", "node_type": "end",
         "config": {"output": "took-{{ Check.branch }}"}},
    ]
    edges = [
        {"source_id": 1, "target_id": 2},
        {"source_id": 2, "target_id": 3, "label": "true"},
        {"source_id": 2, "target_id": 4, "label": "false"},
        {"source_id": 3, "target_id": 5},
    ]
    status, saved = call("PUT", f"/api/workflows/{workflow_id}/graph",
                         {"nodes": nodes, "edges": edges})
    print("graph save:", status, "nodes:", saved.get("node_count"),
          "edges:", saved.get("edge_count"))
    assert status == 200, saved

    status, validation = call("POST", f"/api/workflows/{workflow_id}/validate")
    print("validate:", status, "valid:", validation.get("is_valid"),
          "layers:", validation.get("layers"))
    assert validation["is_valid"], validation

    status, run = call("POST", f"/api/workflows/{workflow_id}/executions",
                       {"wait": True, "input_data": {"threshold": 5}})
    execution_id = run["execution_id"]
    print("run:", status, "status:", run.get("status"))
    print("  completed:", run.get("completed"), "skipped:", run.get("skipped"))
    assert run["status"] == "COMPLETED", run
    # Branch gating: the false branch must not have executed.
    assert 4 in run["skipped"], f"branch gating failed: {run}"
    assert 3 in run["completed"], f"true branch did not run: {run}"

    status, detail = call("GET", f"/api/executions/{execution_id}")
    print("node statuses:",
          {n["node_id"]: n["status"] for n in detail["node_executions"]})

    status, logs = call("GET", f"/api/executions/{execution_id}/logs")
    print("durable logs:", logs["count"])
    assert logs["count"] > 0

    status, timeline = call("GET", f"/api/executions/{execution_id}/timeline")
    print("timeline nodes:", timeline["node_count"])

    status, history = call("GET", f"/api/executions?workflow_id={workflow_id}")
    print("history total:", history["total"])
    assert history["total"] >= 1

    status, replay = call("POST", f"/api/executions/{execution_id}/replay",
                          {"start": False})
    print("replay:", status, "new id:", replay.get("execution_id"),
          "parent:", replay.get("parent_execution_id"))
    assert status == 201

    status, queue = call("GET", "/api/executions/queue")
    print("queue workers running:", queue["workers"]["running"],
          "size:", queue["queue_size"])

    # A finished execution's stream must terminate, not hang on heartbeats.
    started = time.time()
    with urllib.request.urlopen(
        BASE + f"/api/executions/{execution_id}/stream", timeout=20
    ) as response:
        body = response.read().decode()
    elapsed = time.time() - started
    print(f"SSE closed in {elapsed:.2f}s, frames:", body.count("event:"))
    assert "execution.finished" in body
    assert elapsed < 15, "SSE did not terminate promptly"

    print("\nE2E OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
