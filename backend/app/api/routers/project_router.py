"""Project CRUD endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.dependencies import require_read_write
from app.core.errors import NotFoundError
from app.domain.repositories.project_repository import ProjectCreate, ProjectUpdate
from app.infrastructure.database.database import get_db
from app.services.project.project_service import project_service

# Router-level authorization. Applies to every route here, including any
# added later, so a new endpoint cannot ship unprotected by omission.
# Projects are content.
router = APIRouter(prefix="/projects", tags=["Projects"], dependencies=[Depends(require_read_write)])


@router.post("/", summary="Create a project")
def create_project(project_in: ProjectCreate, db: Session = Depends(get_db)):
    return project_service.create_project(db, project_in)


@router.get("/", summary="List projects")
def list_projects(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return project_service.list_projects(db, skip, limit)


@router.get("/{project_id}", summary="Get a project")
def get_project(project_id: int, db: Session = Depends(get_db)):
    project = project_service.get_project(db, project_id)
    if not project:
        raise NotFoundError(f"Project {project_id} not found.")
    return project


@router.put("/{project_id}", summary="Update a project")
def update_project(
    project_id: int, project_in: ProjectUpdate, db: Session = Depends(get_db)
):
    project = project_service.update_project(db, project_id, project_in)
    if not project:
        raise NotFoundError(f"Project {project_id} not found.")
    return project


@router.delete("/{project_id}", status_code=204, summary="Delete a project")
def delete_project(project_id: int, db: Session = Depends(get_db)) -> Response:
    deleted = project_service.delete_project(db, project_id)
    if not deleted:
        raise NotFoundError(f"Project {project_id} not found.")
    return Response(status_code=204)
