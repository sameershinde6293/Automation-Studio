import enum

from sqlalchemy import (
    Column,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.domain.models.base import BaseModel


class ExecutionStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.SKIPPED,
        }


class Workflow(BaseModel):
    __tablename__ = "workflows"
    name = Column(String, index=True, nullable=False)
    description = Column(String, nullable=True)
    version = Column(String, default="1.0.0", nullable=False)

    nodes = relationship(
        "Node",
        back_populates="workflow",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
    edges = relationship(
        "Edge",
        back_populates="workflow",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )
    executions = relationship(
        "WorkflowExecution",
        back_populates="workflow",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Node(BaseModel):
    __tablename__ = "workflow_nodes"
    workflow_id = Column(
        Integer, ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = Column(String, nullable=False)
    node_type = Column(String, nullable=False, index=True)
    config = Column(JSON, nullable=True)
    input_schema = Column(JSON, nullable=True)
    output_schema = Column(JSON, nullable=True)

    # V1.1: canvas geometry for the visual editor.
    position_x = Column(Float, default=0.0, nullable=False)
    position_y = Column(Float, default=0.0, nullable=False)

    # V1.1: per-node execution policy (retries, timeout, on_error, ...).
    retry_policy = Column(JSON, nullable=True)

    workflow = relationship("Workflow", back_populates="nodes")


class Edge(BaseModel):
    __tablename__ = "workflow_edges"
    workflow_id = Column(
        Integer, ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id = Column(
        Integer,
        ForeignKey("workflow_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_id = Column(
        Integer,
        ForeignKey("workflow_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Optional label / branch condition key (e.g. "true", "false").
    label = Column(String, nullable=True)

    workflow = relationship("Workflow", back_populates="edges")


class WorkflowExecution(BaseModel):
    __tablename__ = "workflow_executions"
    workflow_id = Column(
        Integer, ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status = Column(
        SAEnum(ExecutionStatus), default=ExecutionStatus.PENDING, nullable=False, index=True
    )
    state = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    trigger = Column(String, nullable=True)
    started_at = Column(String, nullable=True)
    finished_at = Column(String, nullable=True)

    workflow = relationship("Workflow", back_populates="executions")
    node_executions = relationship(
        "NodeExecution",
        back_populates="execution",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class NodeExecution(BaseModel):
    __tablename__ = "node_executions"
    execution_id = Column(
        Integer,
        ForeignKey("workflow_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id = Column(
        Integer,
        ForeignKey("workflow_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = Column(
        SAEnum(ExecutionStatus), default=ExecutionStatus.PENDING, nullable=False, index=True
    )
    input_data = Column(JSON, nullable=True)
    output_data = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)
    duration_ms = Column(Float, nullable=True)

    execution = relationship("WorkflowExecution", back_populates="node_executions")
