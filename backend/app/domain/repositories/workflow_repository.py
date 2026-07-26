from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.domain.models.workflow import (
    Edge,
    ExecutionLog,
    ExecutionStatus,
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
    # --- M4 ---
    priority: int = 50
    queued_at: Optional[str] = None
    parent_execution_id: Optional[int] = None
    replay_of: Optional[str] = None
    input_data: Optional[Dict[str, Any]] = None
    metrics: Optional[Dict[str, Any]] = None


class NodeExecutionCreate(BaseModel):
    execution_id: int
    node_id: int
    status: Optional[str] = "PENDING"
    input_data: Optional[Dict[str, Any]] = None
    # --- M4 ---
    iteration: int = 0


class ExecutionLogCreate(BaseModel):
    execution_id: int
    message: str
    level: str = "INFO"
    node_id: Optional[int] = None
    sequence: int = 0
    context: Optional[Dict[str, Any]] = None


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

    # ------------------------------------------------------------------ #
    # M4: history search / filtering / stats
    # ------------------------------------------------------------------ #
    def _filtered_query(
        self,
        db: Session,
        *,
        workflow_id: Optional[int] = None,
        statuses: Optional[Sequence[str]] = None,
        trigger: Optional[str] = None,
        search: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ):
        query = db.query(self.model)
        if workflow_id is not None:
            query = query.filter(self.model.workflow_id == workflow_id)
        if statuses:
            normalised = []
            for status in statuses:
                if isinstance(status, ExecutionStatus):
                    normalised.append(status)
                    continue
                key = str(status).strip().upper()
                if key in ExecutionStatus.__members__:
                    normalised.append(ExecutionStatus[key])
            if normalised:
                query = query.filter(self.model.status.in_(normalised))
        if trigger:
            query = query.filter(self.model.trigger == trigger)
        if created_after:
            query = query.filter(self.model.created_at >= created_after)
        if created_before:
            query = query.filter(self.model.created_at <= created_before)
        if search:
            term = f"%{search}%"
            # Match the execution's own error text or its workflow's name.
            matching_workflows = (
                db.query(Workflow.id).filter(Workflow.name.ilike(term)).subquery()
            )
            query = query.filter(
                self.model.error.ilike(term)
                | self.model.workflow_id.in_(db.query(matching_workflows.c.id))
            )
        return query

    def search(
        self,
        db: Session,
        *,
        workflow_id: Optional[int] = None,
        statuses: Optional[Sequence[str]] = None,
        trigger: Optional[str] = None,
        search: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> List[WorkflowExecution]:
        query = self._filtered_query(
            db,
            workflow_id=workflow_id,
            statuses=statuses,
            trigger=trigger,
            search=search,
            created_after=created_after,
            created_before=created_before,
        )
        return query.order_by(self.model.id.desc()).offset(skip).limit(limit).all()

    def count_filtered(
        self,
        db: Session,
        *,
        workflow_id: Optional[int] = None,
        statuses: Optional[Sequence[str]] = None,
        trigger: Optional[str] = None,
        search: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> int:
        return self._filtered_query(
            db,
            workflow_id=workflow_id,
            statuses=statuses,
            trigger=trigger,
            search=search,
            created_after=created_after,
            created_before=created_before,
        ).count()

    def status_counts(
        self, db: Session, workflow_id: Optional[int] = None
    ) -> Dict[str, int]:
        """Aggregate execution counts by status in a single grouped query."""
        query = db.query(self.model.status, func.count(self.model.id))
        if workflow_id is not None:
            query = query.filter(self.model.workflow_id == workflow_id)
        counts: Dict[str, int] = {}
        for status, total in query.group_by(self.model.status).all():
            key = status.value if isinstance(status, ExecutionStatus) else str(status)
            counts[key] = int(total)
        return counts

    def get_claimable(
        self, db: Session, limit: int = 10
    ) -> List[WorkflowExecution]:
        """Queued runs ordered by priority then FIFO (used on worker restart)."""
        return (
            db.query(self.model)
            .filter(
                self.model.status.in_(
                    [ExecutionStatus.PENDING, ExecutionStatus.QUEUED]
                )
            )
            .order_by(self.model.priority.asc(), self.model.id.asc())
            .limit(limit)
            .all()
        )


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

    def delete_by_execution(self, db: Session, execution_id: int) -> int:
        count = (
            db.query(self.model)
            .filter(self.model.execution_id == execution_id)
            .delete(synchronize_session=False)
        )
        db.commit()
        return count


class ExecutionLogRepository(BaseRepository[ExecutionLog, ExecutionLogCreate, BaseModel]):
    def get_by_execution(
        self,
        db: Session,
        execution_id: int,
        *,
        after_sequence: int = 0,
        level: Optional[str] = None,
        node_id: Optional[int] = None,
        search: Optional[str] = None,
        limit: int = 500,
    ) -> List[ExecutionLog]:
        query = db.query(self.model).filter(self.model.execution_id == execution_id)
        if after_sequence:
            query = query.filter(self.model.sequence > after_sequence)
        if level:
            key = str(level).strip().upper()
            from app.domain.models.workflow import LogLevel

            if key in LogLevel.__members__:
                query = query.filter(self.model.level == LogLevel[key])
        if node_id is not None:
            query = query.filter(self.model.node_id == node_id)
        if search:
            query = query.filter(self.model.message.ilike(f"%{search}%"))
        return query.order_by(self.model.sequence.asc()).limit(limit).all()

    def next_sequence(self, db: Session, execution_id: int) -> int:
        current = (
            db.query(func.max(self.model.sequence))
            .filter(self.model.execution_id == execution_id)
            .scalar()
        )
        return int(current or 0) + 1

    def bulk_append(
        self, db: Session, records: Sequence[ExecutionLogCreate]
    ) -> List[ExecutionLog]:
        """Insert many log rows in one transaction (engine flushes in batches)."""
        if not records:
            return []
        rows = [self.model(**record.model_dump()) for record in records]
        db.add_all(rows)
        db.commit()
        return rows

    def count_by_execution(self, db: Session, execution_id: int) -> int:
        return (
            db.query(self.model).filter(self.model.execution_id == execution_id).count()
        )


workflow_repo = WorkflowRepository(Workflow)
node_repo = NodeRepository(Node)
edge_repo = EdgeRepository(Edge)
workflow_execution_repo = WorkflowExecutionRepository(WorkflowExecution)
node_execution_repo = NodeExecutionRepository(NodeExecution)
execution_log_repo = ExecutionLogRepository(ExecutionLog)
