import enum
from sqlalchemy import Column, Integer, String, ForeignKey, JSON, Enum as SAEnum
from app.domain.models.base import BaseModel

class ExecutionStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class Workflow(BaseModel):
    __tablename__ = "workflows"
    name = Column(String, index=True, nullable=False)
    description = Column(String, nullable=True)
    version = Column(String, default="1.0.0", nullable=False)

class Node(BaseModel):
    __tablename__ = "workflow_nodes"
    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=False)
    name = Column(String, nullable=False)
    node_type = Column(String, nullable=False)
    config = Column(JSON, nullable=True)
    input_schema = Column(JSON, nullable=True)
    output_schema = Column(JSON, nullable=True)

class Edge(BaseModel):
    __tablename__ = "workflow_edges"
    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=False)
    source_id = Column(Integer, ForeignKey("workflow_nodes.id"), nullable=False)
    target_id = Column(Integer, ForeignKey("workflow_nodes.id"), nullable=False)

class WorkflowExecution(BaseModel):
    __tablename__ = "workflow_executions"
    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=False)
    status = Column(SAEnum(ExecutionStatus), default=ExecutionStatus.PENDING)
    state = Column(JSON, nullable=True)
    error = Column(String, nullable=True)

class NodeExecution(BaseModel):
    __tablename__ = "node_executions"
    execution_id = Column(Integer, ForeignKey("workflow_executions.id"), nullable=False)
    node_id = Column(Integer, ForeignKey("workflow_nodes.id"), nullable=False)
    status = Column(SAEnum(ExecutionStatus), default=ExecutionStatus.PENDING)
    input_data = Column(JSON, nullable=True)
    output_data = Column(JSON, nullable=True)
    error = Column(String, nullable=True)
    retry_count = Column(Integer, default=0)
