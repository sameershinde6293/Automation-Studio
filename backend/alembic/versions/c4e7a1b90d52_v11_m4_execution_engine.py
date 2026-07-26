"""V1.1 M4: execution queueing, metrics, replay lineage and durable logs

Revision ID: c4e7a1b90d52
Revises: b7d9f8a2c1e3
Create Date: 2026-07-26

Adds everything the M4 execution engine needs:

``workflow_executions``
  - ``priority``            queue ordering (lower dequeues first)
  - ``queued_at``           ISO timestamp of queue admission
  - ``parent_execution_id`` replay/resume lineage
  - ``replay_of``           "replay" | "resume_failed"
  - ``input_data``          caller-supplied run variables
  - ``metrics``             aggregate node/duration/token/cost counters

``node_executions``
  - ``queued_ms``           time spent waiting for a concurrency slot
  - ``started_at`` / ``finished_at``
  - ``iteration``           loop iteration index
  - ``attempt_metrics``     per-attempt timings plus executor counters
  - ``error_code``          stable machine-readable failure classification

``workflow_execution_logs`` (new)
  Durable, sequenced, streamable log records per execution.

All added columns are nullable or server-defaulted so existing rows migrate
cleanly. The ``ExecutionStatus`` enum gains QUEUED/PAUSING/STOPPING; SQLite
stores enums as VARCHAR so no type rewrite is required there, and the
non-SQLite branch extends the native enum type explicitly.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e7a1b90d52"
down_revision: Union[str, Sequence[str], None] = "b7d9f8a2c1e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_STATUSES = ("QUEUED", "PAUSING", "STOPPING")


def _extend_status_enum() -> None:
    """Add the new ExecutionStatus members on databases with native enums."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite/MySQL store SAEnum as VARCHAR with a CHECK constraint that
        # batch_alter_table rebuilds below; nothing to do here.
        return
    for status in NEW_STATUSES:
        op.execute(
            f"ALTER TYPE executionstatus ADD VALUE IF NOT EXISTS '{status}'"
        )


def upgrade() -> None:
    _extend_status_enum()

    with op.batch_alter_table("workflow_executions") as batch:
        batch.add_column(
            sa.Column("priority", sa.Integer(), nullable=False, server_default="50")
        )
        batch.add_column(sa.Column("queued_at", sa.String(), nullable=True))
        batch.add_column(sa.Column("parent_execution_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("replay_of", sa.String(), nullable=True))
        batch.add_column(sa.Column("input_data", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("metrics", sa.JSON(), nullable=True))

    op.create_index(
        "ix_workflow_executions_priority", "workflow_executions", ["priority"]
    )
    op.create_index(
        "ix_workflow_executions_parent_execution_id",
        "workflow_executions",
        ["parent_execution_id"],
    )
    # Composite indices for the two hot paths: history filtering and dequeue.
    op.create_index(
        "ix_workflow_executions_workflow_status",
        "workflow_executions",
        ["workflow_id", "status"],
    )
    op.create_index(
        "ix_workflow_executions_status_priority",
        "workflow_executions",
        ["status", "priority", "id"],
    )

    with op.batch_alter_table("node_executions") as batch:
        batch.add_column(sa.Column("queued_ms", sa.Float(), nullable=True))
        batch.add_column(sa.Column("started_at", sa.String(), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.String(), nullable=True))
        batch.add_column(
            sa.Column("iteration", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("attempt_metrics", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("error_code", sa.String(), nullable=True))

    op.create_index(
        "ix_node_executions_execution_status",
        "node_executions",
        ["execution_id", "status"],
    )

    op.create_table(
        "workflow_execution_logs",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column(
            "execution_id",
            sa.Integer(),
            sa.ForeignKey("workflow_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            sa.Integer(),
            sa.ForeignKey("workflow_nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "level",
            sa.Enum("DEBUG", "INFO", "WARNING", "ERROR", name="loglevel"),
            nullable=False,
            server_default="INFO",
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("context", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_workflow_execution_logs_execution_id",
        "workflow_execution_logs",
        ["execution_id"],
    )
    op.create_index(
        "ix_workflow_execution_logs_node_id", "workflow_execution_logs", ["node_id"]
    )
    op.create_index(
        "ix_workflow_execution_logs_level", "workflow_execution_logs", ["level"]
    )
    op.create_index(
        "ix_execution_logs_execution_sequence",
        "workflow_execution_logs",
        ["execution_id", "sequence"],
    )


def downgrade() -> None:
    for index in (
        "ix_execution_logs_execution_sequence",
        "ix_workflow_execution_logs_level",
        "ix_workflow_execution_logs_node_id",
        "ix_workflow_execution_logs_execution_id",
    ):
        op.drop_index(index, table_name="workflow_execution_logs")
    op.drop_table("workflow_execution_logs")

    op.drop_index("ix_node_executions_execution_status", table_name="node_executions")
    with op.batch_alter_table("node_executions") as batch:
        batch.drop_column("error_code")
        batch.drop_column("attempt_metrics")
        batch.drop_column("iteration")
        batch.drop_column("finished_at")
        batch.drop_column("started_at")
        batch.drop_column("queued_ms")

    for index in (
        "ix_workflow_executions_status_priority",
        "ix_workflow_executions_workflow_status",
        "ix_workflow_executions_parent_execution_id",
        "ix_workflow_executions_priority",
    ):
        op.drop_index(index, table_name="workflow_executions")

    with op.batch_alter_table("workflow_executions") as batch:
        batch.drop_column("metrics")
        batch.drop_column("input_data")
        batch.drop_column("replay_of")
        batch.drop_column("parent_execution_id")
        batch.drop_column("queued_at")
        batch.drop_column("priority")
