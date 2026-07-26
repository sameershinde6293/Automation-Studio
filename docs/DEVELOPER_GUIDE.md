# Developer Guide

## Architecture
- **Frontend:** React + Vite + Electron (TypeScript)
- **Backend:** FastAPI + SQLAlchemy + Alembic (Python)
- **Database:** SQLite (local first)

## Development Workflow
1. Start the Python Backend:
   ```bash
   cd backend
   source venv/bin/activate
   uvicorn app.main:app --reload
   ```
2. Start the Frontend (Electron/Vite):
   ```bash
   cd frontend
   npm run electron:dev
   ```

## Contribution Standards
- Write tests for all new services.
- Ensure `pytest` and `npm run build` pass before committing.
