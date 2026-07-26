from sqlalchemy.orm import Session
from app.domain.repositories.project_repository import project_repo, ProjectCreate, ProjectUpdate
from app.domain.models.project import Project
from typing import List, Optional

class ProjectService:
    def create_project(self, db: Session, project_in: ProjectCreate) -> Project:
        return project_repo.create(db=db, obj_in=project_in)

    def get_project(self, db: Session, project_id: int) -> Optional[Project]:
        return project_repo.get(db=db, id=project_id)

    def list_projects(self, db: Session, skip: int = 0, limit: int = 100) -> List[Project]:
        return project_repo.get_all(db=db, skip=skip, limit=limit)

    def update_project(self, db: Session, project_id: int, project_in: ProjectUpdate) -> Optional[Project]:
        db_project = self.get_project(db, project_id)
        if not db_project:
            return None
        return project_repo.update(db=db, db_obj=db_project, obj_in=project_in)

    def delete_project(self, db: Session, project_id: int) -> Optional[Project]:
        db_project = self.get_project(db, project_id)
        if not db_project:
            return None
        return project_repo.delete(db=db, id=project_id)

project_service = ProjectService()
