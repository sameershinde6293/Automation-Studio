import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.main import app
from app.infrastructure.database.database import Base, get_db

# Setup test database
test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)

client = TestClient(app)

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}

def test_project_endpoints():
    # Create project
    resp = client.post("/api/projects/", json={"name": "API Project", "description": "API Test"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "API Project"
    
    # List projects
    resp = client.get("/api/projects/")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

def test_workflow_endpoints():
    # Create workflow
    resp = client.post("/api/workflows/", json={"name": "API Workflow", "version": "1.0.0"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "API Workflow"
    
    # List workflows
    resp = client.get("/api/workflows/")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
