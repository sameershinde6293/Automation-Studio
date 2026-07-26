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
- `POST /api/workflows/{workflow_id}/run` - Execute a workflow

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
