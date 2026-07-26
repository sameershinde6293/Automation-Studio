# API Documentation

Creator OS exposes a local HTTP API on `http://localhost:8000` when the desktop app is running.

All application errors use the stable envelope:

```json
{"error": {"code": "not_found", "message": "...", "request_id": "..."}}
```

## General

- `GET /` - Root status
- `GET /health` - V1-compatible health check
- `GET /health/live` - Liveness probe
- `GET /health/ready` - Readiness probe with database/scheduler checks
- `GET /api/system/info` - Runtime/build information
- `GET /api/system/metrics` - Lightweight process metrics
- `GET /api/system/node-types` - Workflow node catalog
- `GET /api/system/node-schemas` - Typed input/output schemas per node type (M4)
  - Query: `include_aliases=false`, `category=<control|ai|network|data|script|io|media|integration>`
  - Each entry reports `enabled` (false for flag-gated nodes), `canonical_type` and `is_alias`.
- `GET /api/system/events` - Recent in-process events
- `GET /api/system/scheduler/jobs` - Scheduled background jobs

## Workflows

- `POST /api/workflows/` - Create a workflow
- `GET /api/workflows/` - List/search workflows
- `GET /api/workflows/{workflow_id}` - Fetch a workflow
- `PUT /api/workflows/{workflow_id}` - Update a workflow
- `DELETE /api/workflows/{workflow_id}` - Delete a workflow
- `POST /api/workflows/{workflow_id}/nodes` - Create a node
- `GET /api/workflows/{workflow_id}/nodes` - List nodes
- `PUT /api/workflows/{workflow_id}/nodes/{node_id}` - Update a node
- `DELETE /api/workflows/{workflow_id}/nodes/{node_id}` - Delete a node
- `POST /api/workflows/{workflow_id}/edges` - Create an edge
- `GET /api/workflows/{workflow_id}/edges` - List edges
- `POST /api/workflows/{workflow_id}/graph/validate` - Validate a DAG
- `PUT /api/workflows/{workflow_id}/graph` - Save a complete graph
- `POST /api/workflows/{workflow_id}/executions` - Create and start an execution
  - Body: `{"trigger": str?, "wait": bool?, "priority": int?, "input_data": object?, "queued": bool?}`
  - `priority`: `0` critical, `10` high, `50` normal (default), `90` low. Out-of-range values snap to the nearest band.
  - `input_data` is seeded into the run context and readable as `{{ vars.x }}`.
  - `wait=true` runs synchronously and returns the final summary.
  - Returns `status` (`QUEUED` when the worker pool accepted it, `PENDING` when submitted directly) and a `stream_url`.
  - `429 queue_full` when the queue is at `EXECUTION_QUEUE_MAX_SIZE`.
- `GET /api/workflows/{workflow_id}/executions` - List executions for one workflow
- `GET /api/workflows/executions/{execution_id}` - Execution status and node results
- `POST /api/workflows/executions/{execution_id}/cancel` - Hard cancel
- `POST /api/workflows/{execution_id}/run` - **Deprecated** V1.0 compatibility endpoint

`POST /api/workflows/{workflow_id}/validate` is loop-aware: a cycle whose closing
edge is labelled `loop` is valid. The response adds `loop_edges` and
`node_errors` (per-node config validation), so the editor can surface a bad node
config before the run instead of failing mid-execution.

## Executions (M4)

Execution control, history and streaming. These complement — and do not
duplicate — the workflow-scoped endpoints above.

### Control

- `POST /api/executions/{id}/pause` - Stop scheduling new nodes; in-flight nodes finish
- `POST /api/executions/{id}/resume` - Resume a paused execution
- `POST /api/executions/{id}/stop` - Graceful stop; drains in-flight nodes, ends as `CANCELLED`

All three return `{"execution_id", "action", "changed", "message"}`.
`changed=false` means the request was a no-op (e.g. already paused).
A terminal execution returns `409 conflict`; an unknown id returns `404`.

### History

- `GET /api/executions` - Search across workflows
  - Query: `workflow_id`, `status` (repeatable), `trigger`, `search`, `created_after`, `created_before`, `skip`, `limit` (1-200)
  - `search` matches the workflow name **or** the execution error text.
  - Unknown `status` values return `422`.
  - Response: `{"items": [...], "total": int, "skip": int, "limit": int, "has_more": bool}`
- `GET /api/executions/{id}` - Detail with `node_executions`, `state`, `metrics`, `is_running`, `is_paused`
- `GET /api/executions/{id}/logs` - Durable log records
  - Query: `after_sequence`, `level`, `node_id`, `search`, `limit` (1-2000)
- `GET /api/executions/{id}/timeline` - Node-by-node timings for a Gantt view
- `GET /api/executions/{id}/lineage` - Replay/resume ancestors and children
- `GET /api/executions/stats` - Aggregate counts, success rate, durations, tokens, cost
- `GET /api/executions/queue` - Queue depth, waiting entries, worker pool and streaming stats

### Replay and resume

- `POST /api/executions/{id}/replay` - Fresh run of the same graph (`201`)
  - Body: `{"priority": int?, "input_data": object?, "start": bool?}`
- `POST /api/executions/{id}/resume-failed` - Retry a failed run with prior context (`201`)
  - Only valid for `FAILED`/`CANCELLED` executions; otherwise `409`.
  - Completed node outputs are seeded into `input_data.__resume__`.
  - **Limitation:** the graph is re-traversed from the start, so completed nodes
    re-execute unless they are pure. This is a retry with context, not
    mid-graph resumption.

### Live updates

- `GET /api/executions/{id}/stream` - Server-Sent Events stream
  - Query: `after_sequence` to resume after a reconnect.
  - Events: `execution.queued|started|progress|paused|resumed|stopping|finished`,
    `node.started|finished|retry|skipped`, `log`.
  - Subscriber queues are bounded (drop-oldest), so a slow client is degraded
    rather than stalling the engine.
  - The stream closes on the terminal event; a `: keepalive` comment is sent
    every `EXECUTION_STREAM_HEARTBEAT_SECONDS` while idle.
- `GET /api/executions/{id}/events` - Polling fallback for clients without `EventSource`

## AI Runtime

### Provider and model registry

- `GET /api/ai/providers` - Inspect configured providers and capabilities
- `POST /api/ai/models` - Register a model
  - Body: `{"name": str, "provider": "mock|openai|local", "model_type": "llm|embedding|vision|tts|stt", "config": object?, "is_active": bool?}`
- `GET /api/ai/models?active_only=false` - List models
- `GET /api/ai/models/{model_id}` - Fetch a model
- `PUT /api/ai/models/{model_id}` - Update provider/type/config/active flag
- `DELETE /api/ai/models/{model_id}` - Delete a model

### Conversations and chat

- `POST /api/ai/conversations` - Create a conversation
  - Body: `{"title": str?, "metadata_": object?}`
- `GET /api/ai/conversations` - List conversations
- `GET /api/ai/conversations/{conversation_id}?include_messages=true` - Fetch a conversation and optionally messages
- `PUT /api/ai/conversations/{conversation_id}` - Update title/metadata
- `DELETE /api/ai/conversations/{conversation_id}` - Delete a conversation and its messages
- `POST /api/ai/conversations/{conversation_id}/messages` - Append a message
  - Body: `{"conversation_id": int, "role": "system|user|assistant|tool", "content": str, "tokens_used": int?}`
- `GET /api/ai/conversations/{conversation_id}/messages` - List messages
- `POST /api/ai/chat` - Chat with an active registered model
  - Body: `{"conversation_id": int, "model_name": str, "message": str, "options": object?}`
  - Response includes `response`, persisted message ids, `usage`, `usage_id` and `trimmed_messages`.

The orchestrator trims context by `AI_CONTEXT_MAX_MESSAGES` and
`AI_CONTEXT_MAX_TOKENS`, preserving a leading system prompt and the most recent
turns.

### Cost, tracing and health (M4)

- `POST /api/ai/estimate` - Estimate tokens and USD cost
  - Body: `{"text": str?, "model_name": str?, "prompt_tokens": int?, "completion_tokens": int?}`
  - Requires either `text` or `prompt_tokens`, otherwise `422`.
  - Prices are list-price **estimates**, not billing truth.
- `GET /api/ai/pricing` - Known per-model pricing
- `GET /api/ai/traces?limit=50&only_failures=false` - Recent AI calls including
  every fallback attempt. In-memory and bounded; lost on restart.
- `GET /api/ai/health` - Provider availability, circuit-breaker state and the
  configured fallback chain

Generation used by AI workflow nodes goes through the provider fallback chain
(`AI_FALLBACK_CHAIN`) with a circuit breaker per provider.

### Token usage

- `GET /api/ai/usage` - List token usage rows, optional `model_name`
- `GET /api/ai/usage/summary` - Aggregate prompt/completion/total tokens and request count, optional `model_name`

## Media

All media file access is restricted to `MEDIA_ROOT`. The storage layer rejects
path traversal, absolute path escapes, Windows drive escapes, null-byte paths and
symlink escapes.

### Asset CRUD

- `POST /api/media/assets` - Upload a media file as `multipart/form-data`
  - Query: `process=true` to enqueue processing after upload; `wait=true` to wait for completion.
  - Uploads are streamed with `MEDIA_MAX_FILE_BYTES` enforcement and MIME/media type detection based on file content.
- `POST /api/media/assets/register` - Register an existing file already under `MEDIA_ROOT`
- `GET /api/media/assets` - List assets
- `GET /api/media/assets/{asset_id}` - Fetch asset metadata
- `PUT /api/media/assets/{asset_id}` - Update display filename/metadata/processed flag
- `GET /api/media/assets/{asset_id}/content` - Download asset content
- `DELETE /api/media/assets/{asset_id}` - Delete the asset row and stored file

### Processing and FFmpeg

- `POST /api/media/{asset_id}/process` - Enqueue background processing; returns `202 Accepted`
  - Query: `wait=true` waits for completion for compatibility.
- `GET /api/media/jobs` - List processing jobs
- `GET /api/media/jobs/{job_id}` - Poll job status/progress/result/error
- `POST /api/media/{asset_id}/probe` - Run ffprobe metadata extraction with graceful fallback
- `GET /api/media/ffmpeg` - Inspect configured FFmpeg/ffprobe binaries and availability

Processing uses a bounded worker pool (`MEDIA_MAX_CONCURRENT_JOBS`). Images get
metadata extraction and Pillow posters; video/audio assets use ffprobe and ffmpeg
when available and return structured fallback metadata otherwise.
