# API Documentation

Creator OS exposes a local HTTP API on `http://localhost:8000` when the desktop app is running.

## Endpoints

### General
- `GET /` - Root status
- `GET /health` - Health check

### Workflows
- `POST /api/workflows/` - Create a workflow
- `GET /api/workflows/` - List workflows
- `POST /api/workflows/{execution_id}/run` - Execute a workflow

### AI Runtime
- `POST /api/ai/chat` - Chat with an AI model
  - Body: `{"conversation_id": int, "model_name": str, "message": str}`

### Media
- `POST /api/media/{asset_id}/process` - Process a media asset
