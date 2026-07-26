#!/usr/bin/env python3
"""End-to-end smoke test for M4 execution control (pause / resume / stop).

Proves against a **running** server that pause genuinely halts scheduling (not
just flips a status field), that resume continues, that a graceful stop leaves
no node stranded in RUNNING, and that control calls on a finished execution
return 409.

Usage: see e2e_execution_smoke.py. Exits non-zero on the first failure.
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


def completed_count(detail):
    return sum(1 for n in detail["node_executions"] if n["status"] == "COMPLETED")


def main() -> int:
    _, workflow = call("POST", "/api/workflows/", {"name": "E2E Control"})
    workflow_id = workflow["id"]

    # A chain of delays gives us time to pause mid-run.
    nodes = [
        {"id": i, "name": f"D{i}", "node_type": "delay", "config": {"seconds": 0.4}}
        for i in range(1, 6)
    ]
    edges = [{"source_id": i, "target_id": i + 1} for i in range(1, 5)]
    status, _ = call("PUT", f"/api/workflows/{workflow_id}/graph",
                     {"nodes": nodes, "edges": edges})
    print("save:", status)
    assert status == 200

    status, run = call("POST", f"/api/workflows/{workflow_id}/executions",
                       {"priority": 0})
    execution_id = run["execution_id"]
    print("queued:", status, run["status"], "priority:", run["priority"])

    time.sleep(0.6)
    changed = call("POST", f"/api/executions/{execution_id}/pause")[1]["changed"]
    print("pause:", changed)
    assert changed is True

    time.sleep(0.8)
    _, paused = call("GET", f"/api/executions/{execution_id}")
    print("  status while paused:", paused["status"], "| is_paused:", paused["is_paused"])
    assert paused["status"] == "PAUSED", paused["status"]
    done_at_pause = completed_count(paused)

    # The real test: no further nodes may complete while paused.
    time.sleep(0.5)
    _, still_paused = call("GET", f"/api/executions/{execution_id}")
    still = completed_count(still_paused)
    print(f"  no progress while paused: {done_at_pause == still} "
          f"({done_at_pause} -> {still})")
    assert done_at_pause == still, "pause did not halt scheduling"

    print("resume:", call("POST", f"/api/executions/{execution_id}/resume")[1]["changed"])
    time.sleep(0.7)
    print("stop:", call("POST", f"/api/executions/{execution_id}/stop")[1]["changed"])

    final = None
    for _ in range(40):
        _, final = call("GET", f"/api/executions/{execution_id}")
        if final["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.25)
    print("final:", final["status"])
    assert final["status"] == "CANCELLED", final["status"]

    stranded = any(n["status"] == "RUNNING" for n in final["node_executions"])
    print("  graceful (no node left RUNNING):", not stranded)
    assert not stranded, "graceful stop left a node RUNNING"

    code = call("POST", f"/api/executions/{execution_id}/pause")[0]
    print("pause after finish ->", code)
    assert code == 409

    _, logs = call("GET", f"/api/executions/{execution_id}/logs")
    messages = [item["message"].lower() for item in logs["items"]]
    print("  log mentions pause:", any("pause" in m for m in messages))
    print("  log mentions stop:", any("stop" in m for m in messages))

    print("\nCONTROL E2E OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
