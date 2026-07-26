import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.infrastructure.database.database import Base
from app.domain.models.project import Project
from app.domain.repositories.project_repository import project_repo, ProjectCreate

engine = create_engine("sqlite:///:memory:")
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)

def test_project_repository(db):
    project_in = ProjectCreate(name="Test Project", description="A test project")
    project = project_repo.create(db=db, obj_in=project_in)
    
    assert project.id is not None
    assert project.name == "Test Project"
    
    fetched_project = project_repo.get(db=db, id=project.id)
    assert fetched_project.name == "Test Project"
