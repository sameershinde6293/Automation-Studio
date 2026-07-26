import enum

from sqlalchemy import (
    Column,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.domain.models.base import BaseModel


class ExecutionStatus(str, enum.Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    PAUSING = "PAUSING"
    STOPPING = "STOPPING"
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

    @property
    def is_active(self) -> bool:
        """True while the execution still occupies a worker or queue slot."""
        return self in {
            ExecutionStatus.PENDING,
            ExecutionStatus.QUEUED,
            ExecutionStatus.RUNNING,
            ExecutionStatus.PAUSED,
            ExecutionStatus.PAUSING,
            ExecutionStatus.STOPPING,
        }


class ExecutionPriority(int, enum.Enum):
    """Lower numeric value is dequeued first."""

    CRITICAL = 0
    HIGH = 10
    NORMAL = 50
    LOW = 90

    @classmethod
    def coerce(cls, value) -> "ExecutionPriority":
        """Map an int/str/None onto the nearest valid priority."""
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.NORMAL
        if isinstance(value, str):
            key = value.strip().upper()
            if key in cls.__members__:
                return cls[key]
            try:
                value = int(key)
            except ValueError:
                return cls.NORMAL
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return cls.NORMAL
        return min(cls, key=lambda member: abs(member.value - numeric))


class LogLevel(str, enum.Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


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

    # --- M4: queueing, replay lineage, inputs and metrics -------------------
    #: Lower value dequeues first (see ``ExecutionPriority``).
    priority = Column(Integer, default=ExecutionPriority.NORMAL.value, nullable=False, index=True)
    #: ISO timestamp of when the run entered the queue.
    queued_at = Column(String, nullable=True)
    #: Set when this run was produced by replaying or resuming another run.
    parent_execution_id = Column(
        Integer,
        ForeignKey("workflow_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: "replay" | "resume_failed" | None
    replay_of = Column(String, nullable=True)
    #: Caller-supplied variables seeded into the execution context.
    input_data = Column(JSON, nullable=True)
    #: Aggregate metrics (node counts, durations, tokens, cost).
    metrics = Column(JSON, nullable=True)

    workflow = relationship("Workflow", back_populates="executions")
    node_executions = relationship(
        "NodeExecution",
        back_populates="execution",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    logs = relationship(
        "ExecutionLog",
        back_populates="execution",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # Hot path for the history panel: filter by workflow then status.
        Index("ix_workflow_executions_workflow_status", "workflow_id", "status"),
        # Hot path for the queue: pick the highest priority pending run.
        Index("ix_workflow_executions_status_priority", "status", "priority", "id"),
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

    # --- M4: fine-grained node metrics -------------------------------------
    #: Time spent waiting for a concurrency slot before execution began.
    queued_ms = Column(Float, nullable=True)
    started_at = Column(String, nullable=True)
    finished_at = Column(String, nullable=True)
    #: Loop iteration index for nodes executed inside a loop body.
    iteration = Column(Integer, default=0, nullable=False)
    #: Per-attempt timings/errors plus any executor-reported counters
    #: (e.g. AI tokens and cost).
    attempt_metrics = Column(JSON, nullable=True)
    #: Stable machine-readable failure classification (see NodeErrorCode).
    error_code = Column(String, nullable=True)

    execution = relationship("WorkflowExecution", back_populates="node_executions")

    __table_args__ = (
        Index("ix_node_executions_execution_status", "execution_id", "status"),
    )


class ExecutionLog(BaseModel):
    """Durable, streamable log record emitted during a workflow run.

    Persisted (rather than only pushed to the Python logger) so the UI log
    viewer can page through history and so a reconnecting SSE client can
    replay what it missed.
    """

    __tablename__ = "workflow_execution_logs"
    execution_id = Column(
        Integer,
        ForeignKey("workflow_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Nullable: engine-level records are not attached to a node.
    node_id = Column(
        Integer,
        ForeignKey("workflow_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    level = Column(SAEnum(LogLevel), default=LogLevel.INFO, nullable=False, index=True)
    message = Column(Text, nullable=False)
    #: Monotonic per-execution counter so clients can resume a stream exactly.
    sequence = Column(Integer, default=0, nullable=False)
    #: Arbitrary structured payload (event name, durations, attempt number...).
    context = Column(JSON, nullable=True)

    execution = relationship("WorkflowExecution", back_populates="logs")

    __table_args__ = (
        Index("ix_execution_logs_execution_sequence", "execution_id", "sequence"),
    )
