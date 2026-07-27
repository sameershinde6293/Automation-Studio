#!/usr/bin/env python3
"""Import, execute and export every example workflow against a live backend.

Shipping example files that nobody runs is how documentation rots: the M5
audit found 20 node components committed as zero-byte files precisely because
nothing loaded them. This script makes the examples executable evidence.

For each file in ``examples/workflows``:

  1. create the workflow            POST /api/workflows/
  2. import the graph               PUT  /api/workflows/{id}/graph
  3. validate it                    POST /api/workflows/{id}/validate
  4. run it synchronously           POST /api/workflows/{id}/executions
  5. read the execution back        GET  /api/workflows/executions/{exec_id}
  6. export the graph               GET  /api/workflows/{id}/graph
  7. assert the export round-trips to the same node and edge counts

Usage
-----
    # start the backend first, then:
    python scripts/verify_examples.py
    python scripts/verify_examples.py --base-url http://127.0.0.1:8000
    python scripts/verify_examples.py --token "$ACCESS_TOKEN"   # if AUTH_ENABLED
    python scripts/verify_examples.py --skip-network            # CI-safe subset

Exit code is non-zero if any example fails, so it can gate a release.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples" / "workflows"

#: Examples that reach the public internet. Skipped with --skip-network so the
#: script stays usable in an air-gapped CI runner.
NETWORK_EXAMPLES = {"03-resilient-http-sync.json"}


class Client:
    def __init__(self, base_url: str, token: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read().decode()
                return resp.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, body
        except urllib.error.URLError as exc:
            raise SystemExit(
                f"Cannot reach {url}: {exc.reason}\n"
                "Start the backend first (uvicorn app.main:app) or pass --base-url."
            )


def verify_one(client: Client, path: Path) -> Tuple[bool, List[str]]:
    """Run the full lifecycle for one example. Returns (ok, messages)."""
    notes: List[str] = []
    spec = json.loads(path.read_text(encoding="utf-8"))

    # 1. create -----------------------------------------------------------
    status, created = client.request(
        "POST",
        "/api/workflows/",
        {
            "name": spec["name"],
            "description": spec.get("description", ""),
            "version": spec.get("version", "1.0.0"),
        },
    )
    if status not in (200, 201):
        return False, [f"create failed: HTTP {status} {created}"]
    workflow_id = created["id"]
    notes.append(f"created id={workflow_id}")

    # 2. import the graph -------------------------------------------------
    status, imported = client.request(
        "PUT",
        f"/api/workflows/{workflow_id}/graph",
        {"nodes": spec["nodes"], "edges": spec["edges"]},
    )
    if status != 200:
        return False, notes + [f"import failed: HTTP {status} {imported}"]
    if imported["node_count"] != len(spec["nodes"]):
        return False, notes + [
            f"imported {imported['node_count']} nodes, file has {len(spec['nodes'])}"
        ]
    if imported["edge_count"] != len(spec["edges"]):
        return False, notes + [
            f"imported {imported['edge_count']} edges, file has {len(spec['edges'])}"
        ]
    notes.append(f"imported {imported['node_count']}n/{imported['edge_count']}e")

    # 3. validate ---------------------------------------------------------
    status, validation = client.request(
        "POST", f"/api/workflows/{workflow_id}/validate"
    )
    if status != 200 or not validation.get("is_valid"):
        return False, notes + [f"validation failed: {validation}"]
    notes.append(f"valid ({len(validation.get('layers', []))} layers)")

    # 4. execute ----------------------------------------------------------
    status, run = client.request(
        "POST", f"/api/workflows/{workflow_id}/executions", {"wait": True}
    )
    if status != 200:
        return False, notes + [f"execute failed: HTTP {status} {run}"]
    run_status = str(run.get("status", "")).upper()
    if run_status != "COMPLETED":
        return False, notes + [
            f"execution {run_status}: {run.get('errors') or run}"
        ]
    notes.append(
        f"executed {len(run.get('completed', []))} nodes in "
        f"{run.get('duration_ms', 0):.0f}ms"
    )

    # 5. read the execution back -----------------------------------------
    execution_id = run["execution_id"]
    status, detail = client.request(
        "GET", f"/api/workflows/executions/{execution_id}"
    )
    if status != 200:
        return False, notes + [f"execution read-back failed: HTTP {status}"]
    if str(detail.get("status", "")).upper() != "COMPLETED":
        return False, notes + [f"persisted status is {detail.get('status')}"]

    # 6/7. export and round-trip -----------------------------------------
    status, exported = client.request("GET", f"/api/workflows/{workflow_id}/graph")
    if status != 200:
        return False, notes + [f"export failed: HTTP {status}"]
    if len(exported["nodes"]) != len(spec["nodes"]):
        return False, notes + [
            f"export has {len(exported['nodes'])} nodes, expected {len(spec['nodes'])}"
        ]
    if len(exported["edges"]) != len(spec["edges"]):
        return False, notes + [
            f"export has {len(exported['edges'])} edges, expected {len(spec['edges'])}"
        ]
    exported_types = sorted(n["node_type"] for n in exported["nodes"])
    source_types = sorted(n["node_type"] for n in spec["nodes"])
    if exported_types != source_types:
        return False, notes + ["export node types differ from the source file"]
    notes.append("export round-trips")

    return True, notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=None, help="Bearer token if AUTH_ENABLED")
    parser.add_argument(
        "--skip-network",
        action="store_true",
        help="skip examples that call the public internet",
    )
    args = parser.parse_args()

    if not EXAMPLES_DIR.is_dir():
        print(f"No examples directory at {EXAMPLES_DIR}", file=sys.stderr)
        return 1

    client = Client(args.base_url, args.token)
    status, _ = client.request("GET", "/health/live")
    if status != 200:
        print(f"Backend not healthy at {args.base_url} (HTTP {status})", file=sys.stderr)
        return 1

    files = sorted(EXAMPLES_DIR.glob("*.json"))
    if not files:
        print("No example workflows found.", file=sys.stderr)
        return 1

    failures = 0
    skipped = 0
    for path in files:
        if args.skip_network and path.name in NETWORK_EXAMPLES:
            print(f"SKIP  {path.name}  (needs network)")
            skipped += 1
            continue
        try:
            ok, notes = verify_one(client, path)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - report, do not abort the batch
            ok, notes = False, [f"unexpected error: {exc!r}"]
        marker = "PASS" if ok else "FAIL"
        print(f"{marker}  {path.name}")
        for note in notes:
            print(f"        {note}")
        if not ok:
            failures += 1

    total = len(files) - skipped
    print(
        f"\n{total - failures}/{total} examples passed"
        + (f", {skipped} skipped" if skipped else "")
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
