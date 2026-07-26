from app.domain.repositories.base_repository import BaseRepository
from app.domain.models.project import Project
from pydantic import BaseModel
from typing import Optional

class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

class ProjectRepository(BaseRepository[Project, ProjectCreate, ProjectUpdate]):
    pass

project_repo = ProjectRepository(Project)
