from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.domain.models.workflow import (
    Edge,
    Node,
    NodeExecution,
    Workflow,
    WorkflowExecution,
)
from app.domain.repositories.base_repository import BaseRepository


class WorkflowCreate(BaseModel):
    name: str
    description: Optional[str] = None
    version: str = "1.0.0"


class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    version: Optional[str] = None


class NodeCreate(BaseModel):
    workflow_id: int
    name: str
    node_type: str
    config: Optional[Dict[str, Any]] = None
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None
    position_x: float = 0.0
    position_y: float = 0.0
    retry_policy: Optional[Dict[str, Any]] = None


class NodeUpdate(BaseModel):
    name: Optional[str] = None
    node_type: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None
    position_x: Optional[float] = None
    position_y: Optional[float] = None
    retry_policy: Optional[Dict[str, Any]] = None


class EdgeCreate(BaseModel):
    workflow_id: int
    source_id: int
    target_id: int
    label: Optional[str] = None


class WorkflowExecutionCreate(BaseModel):
    workflow_id: int
    status: Optional[str] = "PENDING"
    state: Optional[Dict[str, Any]] = None
    trigger: Optional[str] = None


class NodeExecutionCreate(BaseModel):
    execution_id: int
    node_id: int
    status: Optional[str] = "PENDING"
    input_data: Optional[Dict[str, Any]] = None


class WorkflowRepository(BaseRepository[Workflow, WorkflowCreate, WorkflowUpdate]):
    def search_by_name(
        self, db: Session, term: str, skip: int = 0, limit: int = 100
    ) -> List[Workflow]:
        return (
            db.query(self.model)
            .filter(self.model.name.ilike(f"%{term}%"))
            .order_by(self.model.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )


class NodeRepository(BaseRepository[Node, NodeCreate, NodeUpdate]):
    def get_by_workflow(self, db: Session, workflow_id: int) -> List[Node]:
        return (
            db.query(self.model)
            .filter(self.model.workflow_id == workflow_id)
            .order_by(self.model.id)
            .all()
        )

    def delete_by_workflow(self, db: Session, workflow_id: int) -> int:
        count = (
            db.query(self.model)
            .filter(self.model.workflow_id == workflow_id)
            .delete(synchronize_session=False)
        )
        db.commit()
        return count


class EdgeRepository(BaseRepository[Edge, EdgeCreate, BaseModel]):
    def get_by_workflow(self, db: Session, workflow_id: int) -> List[Edge]:
        return (
            db.query(self.model)
            .filter(self.model.workflow_id == workflow_id)
            .order_by(self.model.id)
            .all()
        )

    def delete_by_workflow(self, db: Session, workflow_id: int) -> int:
        count = (
            db.query(self.model)
            .filter(self.model.workflow_id == workflow_id)
            .delete(synchronize_session=False)
        )
        db.commit()
        return count

    def delete_by_node(self, db: Session, node_id: int) -> int:
        count = (
            db.query(self.model)
            .filter(
                (self.model.source_id == node_id) | (self.model.target_id == node_id)
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return count


class WorkflowExecutionRepository(
    BaseRepository[WorkflowExecution, WorkflowExecutionCreate, BaseModel]
):
    def get_by_workflow(
        self, db: Session, workflow_id: int, skip: int = 0, limit: int = 50
    ) -> List[WorkflowExecution]:
        return (
            db.query(self.model)
            .filter(self.model.workflow_id == workflow_id)
            .order_by(self.model.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_recent(self, db: Session, limit: int = 20) -> List[WorkflowExecution]:
        return db.query(self.model).order_by(self.model.id.desc()).limit(limit).all()


class NodeExecutionRepository(BaseRepository[NodeExecution, NodeExecutionCreate, BaseModel]):
    def get_by_execution_and_node(
        self, db: Session, execution_id: int, node_id: int
    ) -> Optional[NodeExecution]:
        return (
            db.query(self.model)
            .filter(
                self.model.execution_id == execution_id,
                self.model.node_id == node_id,
            )
            .first()
        )

    def get_by_execution(self, db: Session, execution_id: int) -> List[NodeExecution]:
        return (
            db.query(self.model)
            .filter(self.model.execution_id == execution_id)
            .order_by(self.model.id)
            .all()
        )


workflow_repo = WorkflowRepository(Workflow)
node_repo = NodeRepository(Node)
edge_repo = EdgeRepository(Edge)
workflow_execution_repo = WorkflowExecutionRepository(WorkflowExecution)
node_execution_repo = NodeExecutionRepository(NodeExecution)
