# Example workflows

Four production-shaped workflows that exercise the engine end to end. Every one
of them is **executed against a live backend** by
`scripts/verify_examples.py` — they are regression evidence, not illustrations.

| File | Demonstrates | Needs | Runtime |
| --- | --- | --- | --- |
| `01-hello-automation.json` | Variables, templating, branching, structured output | nothing | ~1 s |
| `02-ai-content-pipeline.json` | Prompt templates, chained AI calls, provider fallback, retries | nothing (falls back to the `mock` provider) | ~5 s |
| `03-resilient-http-sync.json` | HTTP with timeouts, retry/backoff, success-vs-failure branching, alerting | outbound network | ~10 s |
| `04-scheduled-batch-report.json` | Loop/fan-out, aggregation, pacing, scheduled triggering | nothing | ~3 s |

## Verification status

Last run on 2026-07-28 (M10) against Creator OS 1.1.0, executed on an
**authenticated production backend on PostgreSQL 16.2**:

```
PASS  01-hello-automation.json       6 nodes / 6 edges   5 executed
PASS  02-ai-content-pipeline.json    5 nodes / 4 edges   5 executed  321 ms
PASS  03-resilient-http-sync.json    7 nodes / 7 edges   5 executed  446 ms
PASS  04-scheduled-batch-report.json 5 nodes / 4 edges   5 executed  536 ms

4/4 examples passed
```

> `03-resilient-http-sync.json` makes a real outbound HTTPS request. Behind a
> TLS-inspecting proxy it fails with `CERTIFICATE_VERIFY_FAILED` unless the
> **backend process** is started with
> `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`. Setting that variable on
> `verify_examples.py` has no effect — the request is made server-side (M10-F1).

Each run imports the graph, validates it, executes it synchronously, reads the
execution back, exports the graph and asserts the export round-trips.

## Running them

```bash
# 1. start a backend
cd backend && ./.venv/bin/uvicorn app.main:app --port 8000

# 2. import, run and export every example
python scripts/verify_examples.py

# options
python scripts/verify_examples.py --base-url http://127.0.0.1:8000
python scripts/verify_examples.py --token "$ACCESS_TOKEN"   # when AUTH_ENABLED=true
python scripts/verify_examples.py --skip-network            # air-gapped runners
```

The script exits non-zero on any failure, so it can gate a release.

## Importing one by hand

```bash
WF=$(curl -s -X POST http://localhost:8000/api/workflows/ \
      -H 'Content-Type: application/json' \
      -d '{"name":"Hello Automation"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

python3 - "$WF" <<'PY'
import json, sys, urllib.request
wf = sys.argv[1]
spec = json.load(open("examples/workflows/01-hello-automation.json"))
body = json.dumps({"nodes": spec["nodes"], "edges": spec["edges"]}).encode()
req = urllib.request.Request(
    f"http://localhost:8000/api/workflows/{wf}/graph",
    data=body, method="PUT", headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
PY

curl -s -X POST "http://localhost:8000/api/workflows/$WF/executions" \
  -H 'Content-Type: application/json' -d '{"wait":true}'
```

In the desktop/browser UI: **Workflows → Import** and choose the file.

## Template reference syntax

This trips people up, so it is worth stating precisely. Variables seeded by the
`start` node are reached **through the node**, not as bare names:

```
{{ Start.variables.my_var }}     correct
{{ my_var }}                     empty — a bare name is not a run variable
{{ NodeName.field }}             any upstream node's output field
{{ item }}                       the current element inside a loop node
```

`{{ Start.variables.x }}` and `{{ variables.x }}` both resolve; the examples use
the explicit form because it says where the value came from.

## Scheduling

Creator OS has no inbound trigger node (`KNOWN_ISSUES.md` #15). Runs are started
by calling the API, so use whatever scheduler you already operate.

```cron
# every day at 06:00 — workflow 4
0 6 * * * curl -fsS -X POST http://localhost:8000/api/workflows/4/executions \
            -H 'Content-Type: application/json' -d '{"queued":true}' >> /var/log/creator-os-cron.log 2>&1
```

With authentication enabled, mint a long-lived API key and send it as
`X-API-Key` rather than embedding a password in crontab:

```bash
curl -fsS -X POST http://localhost:8000/api/workflows/4/executions \
  -H "X-API-Key: $CREATOR_OS_API_KEY" \
  -H 'Content-Type: application/json' -d '{"queued":true}'
```

`{"wait":true}` blocks until the run finishes and returns the result — good for
cron, where you want a non-zero exit on failure. `{"queued":true}` returns
immediately and runs through the priority queue. Note that queued runs do **not**
survive a restart (`KNOWN_ISSUES.md` #1).

## Error handling

`03-resilient-http-sync.json` is the reference. The pattern is:

1. **Bound the call** — `timeout` on the node, so a hung endpoint cannot stall the run.
2. **Retry transient failures** — `retry_policy` with `max_attempts` and `backoff_seconds`.
3. **Branch on the outcome** — a `condition` node on `status_code`, rather than letting the run die.
4. **Take an explicit failure path** — record the reason, then notify.

Nodes that raise without a failure branch fail the whole execution, which is the
right default; the example shows how to opt out of it where a partial result is
more useful than a dead run.

## Logging and observability

Every node emits structured log lines carrying `execution_id` and `node_id`.

```bash
# live stream (Server-Sent Events)
curl -N http://localhost:8000/api/executions/<execution_id>/stream

# persisted logs
curl -s "http://localhost:8000/api/executions/<execution_id>/logs" | python3 -m json.tool
```

Set `LOG_FORMAT=json` for machine-readable output; credentials are redacted
before anything is written (verified in M7 — the admin password and database
password appear nowhere in the log file).

## Export / import round trip

`GET /api/workflows/{id}/graph` returns the same shape these files use, so an
export can be committed straight back into `examples/` or moved between
instances. The verification harness asserts the round trip on every run: node
count, edge count and the full set of node types must match the source file.

## A caveat worth knowing

Behind a **TLS-inspecting proxy**, example 03 fails with
`CERTIFICATE_VERIFY_FAILED` even though `curl` to the same URL succeeds — the
backend's HTTP client validates against the `certifi` bundle, which does not
contain the proxy's CA. It is a trust-store mismatch, not a defect:

```bash
SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt ./.venv/bin/uvicorn app.main:app
```

This was hit and resolved during M7 validation; see `docs/TROUBLESHOOTING.md`.
