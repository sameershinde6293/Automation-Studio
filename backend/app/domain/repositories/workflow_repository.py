from app.domain.repositories.base_repository import BaseRepository
from app.domain.models.workflow import Workflow, Node, Edge, WorkflowExecution, NodeExecution
from pydantic import BaseModel
from typing import Optional, Dict, Any, List

class WorkflowCreate(BaseModel):
    name: str
    description: Optional[str] = None
    version: str = "1.0.0"

class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

class NodeCreate(BaseModel):
    workflow_id: int
    name: str
    node_type: str
    config: Optional[Dict[str, Any]] = None
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None

class EdgeCreate(BaseModel):
    workflow_id: int
    source_id: int
    target_id: int

class WorkflowExecutionCreate(BaseModel):
    workflow_id: int
    status: Optional[str] = "PENDING"
    state: Optional[Dict[str, Any]] = None

class NodeExecutionCreate(BaseModel):
    execution_id: int
    node_id: int
    status: Optional[str] = "PENDING"
    input_data: Optional[Dict[str, Any]] = None

class WorkflowRepository(BaseRepository[Workflow, WorkflowCreate, WorkflowUpdate]):
    pass

class NodeRepository(BaseRepository[Node, NodeCreate, BaseModel]):
    def get_by_workflow(self, db, workflow_id: int) -> List[Node]:
        return db.query(self.model).filter(self.model.workflow_id == workflow_id).all()

class EdgeRepository(BaseRepository[Edge, EdgeCreate, BaseModel]):
    def get_by_workflow(self, db, workflow_id: int) -> List[Edge]:
        return db.query(self.model).filter(self.model.workflow_id == workflow_id).all()

class WorkflowExecutionRepository(BaseRepository[WorkflowExecution, WorkflowExecutionCreate, BaseModel]):
    pass

class NodeExecutionRepository(BaseRepository[NodeExecution, NodeExecutionCreate, BaseModel]):
    def get_by_execution_and_node(self, db, execution_id: int, node_id: int):
        return db.query(self.model).filter(
            self.model.execution_id == execution_id,
            self.model.node_id == node_id
        ).first()

workflow_repo = WorkflowRepository(Workflow)
node_repo = NodeRepository(Node)
edge_repo = EdgeRepository(Edge)
workflow_execution_repo = WorkflowExecutionRepository(WorkflowExecution)
node_execution_repo = NodeExecutionRepository(NodeExecution)
