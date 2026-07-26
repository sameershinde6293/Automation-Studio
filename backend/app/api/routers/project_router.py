from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.infrastructure.database.database import get_db
from app.domain.repositories.project_repository import ProjectCreate
from app.services.project.project_service import project_service

router = APIRouter(prefix="/projects", tags=["Projects"])

@router.post("/")
def create_project(project_in: ProjectCreate, db: Session = Depends(get_db)):
    return project_service.create_project(db, project_in)

@router.get("/")
def list_projects(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return project_service.list_projects(db, skip, limit)
