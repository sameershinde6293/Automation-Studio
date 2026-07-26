# Architecture Decisions

## Tech Stack
- **Frontend / Desktop UI**: Electron + React (Vite)
- **Backend / AI Engine**: Python + FastAPI
- **Database**: SQLite (local first) / SQLAlchemy
- **Workflow Engine**: Python Asyncio based custom engine
- **Plugin SDK**: Python & JS interfaces

## Directory Structure
- `frontend/`: Electron and React source code.
- `backend/`: Python FastAPI source code.
- `shared/`: Shared assets, schema definitions (if any).
- `docs/`: Project documentation.
- `scripts/`: Build and deployment scripts.

## Rationale
Building a desktop automation/AI tool (Creator OS) requires access to local system resources (Electron) and robust AI/automation libraries (Python).
