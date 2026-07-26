# Architecture Decisions

## Tech Stack
- **Frontend / Desktop UI**: Electron + React (Vite)
- **Backend / AI Engine**: Python + FastAPI
- **Database**: SQLite (local first) / SQLAlchemy
- **Database Migrations**: Alembic
- **Workflow Engine**: Python Asyncio based custom engine
- **Plugin SDK**: Python & JS interfaces
- **Scheduler**: APScheduler

## Directory Structure
- `frontend/`: Electron and React source code.
- `backend/app/`: FastAPI application code (Domain, Infrastructure, Services).
- `shared/`: Shared assets.
- `docs/`: Project documentation.
- `scripts/`: Build and deployment scripts.

## Rationale
Using a standard Clean Architecture approach allows us to mock dependencies easily and isolate our domain logic from infrastructure details like the specific ORM or UI framework.
