from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.infrastructure.database.database import get_db
from app.services.project.project_service import project_service
from app.domain.repositories.workflow_repository import workflow_repo, WorkflowCreate
from app.services.workflow.engine import workflow_engine

router = APIRouter(prefix="/workflows", tags=["Workflows"])

@router.post("/")
def create_workflow(workflow_in: WorkflowCreate, db: Session = Depends(get_db)):
    return workflow_repo.create(db, workflow_in)

@router.get("/")
def list_workflows(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return workflow_repo.get_all(db, skip, limit)

@router.post("/{execution_id}/run")
async def run_workflow(execution_id: int):
    # This fires off the background task
    workflow_engine.submit(execution_id)
    return {"status": "Execution submitted", "execution_id": execution_id}
