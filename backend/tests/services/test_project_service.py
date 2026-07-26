import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.infrastructure.database.database import Base
from app.services.project.project_service import project_service
from app.domain.repositories.project_repository import ProjectCreate, ProjectUpdate

engine = create_engine("sqlite:///:memory:")
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)

def test_create_and_get_project(db):
    project_in = ProjectCreate(name="Service Test", description="Desc")
    project = project_service.create_project(db, project_in)
    
    assert project.id is not None
    assert project.name == "Service Test"
    
    fetched = project_service.get_project(db, project.id)
    assert fetched.id == project.id

def test_update_project(db):
    project_in = ProjectCreate(name="Old Name", description="Desc")
    project = project_service.create_project(db, project_in)
    
    updated = project_service.update_project(db, project.id, ProjectUpdate(name="New Name"))
    assert updated.name == "New Name"

def test_delete_project(db):
    project_in = ProjectCreate(name="To Delete", description="Desc")
    project = project_service.create_project(db, project_in)
    
    deleted = project_service.delete_project(db, project.id)
    assert deleted.id == project.id
    
    fetched = project_service.get_project(db, project.id)
    assert fetched is None
