import pytest
import asyncio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.infrastructure.database.database import Base
from app.domain.models.project import Project
from app.domain.models.plugin import Plugin
from app.domain.models.workflow import ExecutionStatus, Workflow, Node, Edge, WorkflowExecution, NodeExecution
from app.domain.repositories.workflow_repository import (
    workflow_repo, node_repo, edge_repo, workflow_execution_repo,
    WorkflowCreate, NodeCreate, EdgeCreate, WorkflowExecutionCreate
)
from app.services.workflow.engine import workflow_engine

test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

@pytest.fixture(autouse=True)
def override_db(monkeypatch):
    Base.metadata.create_all(bind=test_engine)
    monkeypatch.setattr("app.services.workflow.engine.SessionLocal", TestingSessionLocal)
    yield
    Base.metadata.drop_all(bind=test_engine)

@pytest.mark.asyncio
async def test_workflow_engine_dag():
    db = TestingSessionLocal()
    wf = workflow_repo.create(db, WorkflowCreate(name="Test WF"))
    n1 = node_repo.create(db, NodeCreate(workflow_id=wf.id, name="Start", node_type="math_add", config={"a": 10, "b": 5}))
    n2 = node_repo.create(db, NodeCreate(workflow_id=wf.id, name="Middle 1", node_type="math_add", config={"a": 1, "b": 2}))
    n3 = node_repo.create(db, NodeCreate(workflow_id=wf.id, name="Middle 2", node_type="math_add", config={"a": 1, "b": 3}))
    edge_repo.create(db, EdgeCreate(workflow_id=wf.id, source_id=1, target_id=2))
    edge_repo.create(db, EdgeCreate(workflow_id=wf.id, source_id=1, target_id=3))
    exec = workflow_execution_repo.create(db, WorkflowExecutionCreate(workflow_id=wf.id))
    db.close()
    
    await workflow_engine.run_execution(exec.id)
    
    db = TestingSessionLocal()
    final_exec = workflow_execution_repo.get(db, exec.id)
    assert final_exec.status == ExecutionStatus.COMPLETED
    
    from app.domain.repositories.workflow_repository import node_execution_repo
    ne1 = node_execution_repo.get_by_execution_and_node(db, exec.id, 1)
    ne2 = node_execution_repo.get_by_execution_and_node(db, exec.id, 2)
    ne3 = node_execution_repo.get_by_execution_and_node(db, exec.id, 3)
    
    assert ne1.output_data["result"] == 15
    assert ne2.output_data["result"] == 17
    assert ne3.output_data["result"] == 18
    db.close()
