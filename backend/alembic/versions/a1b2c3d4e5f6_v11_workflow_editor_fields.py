"""V1.1: workflow editor geometry, per-node retry policy, execution metadata

Revision ID: a1b2c3d4e5f6
Revises: 6530deb854c5
Create Date: 2026-07-26

Adds the columns introduced in Creator OS V1.1:
- ``workflow_nodes.position_x`` / ``position_y`` — canvas geometry for the
  drag-and-drop editor.
- ``workflow_nodes.retry_policy`` — per-node retries/timeout/on_error.
- ``workflow_edges.label`` — branch labels.
- ``workflow_executions.trigger`` / ``started_at`` / ``finished_at``.
- ``node_executions.duration_ms``.

All columns are nullable or defaulted, so existing rows migrate cleanly and
V1.0 data remains valid.
"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "6530deb854c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_nodes") as batch:
        batch.add_column(
            sa.Column("position_x", sa.Float(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("position_y", sa.Float(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("retry_policy", sa.JSON(), nullable=True))

    with op.batch_alter_table("workflow_edges") as batch:
        batch.add_column(sa.Column("label", sa.String(), nullable=True))

    with op.batch_alter_table("workflow_executions") as batch:
        batch.add_column(sa.Column("trigger", sa.String(), nullable=True))
        batch.add_column(sa.Column("started_at", sa.String(), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.String(), nullable=True))

    with op.batch_alter_table("node_executions") as batch:
        batch.add_column(sa.Column("duration_ms", sa.Float(), nullable=True))

    # Indices for the hot query paths exercised by the editor and engine.
    op.create_index(
        "ix_workflow_nodes_workflow_id", "workflow_nodes", ["workflow_id"]
    )
    op.create_index("ix_workflow_nodes_node_type", "workflow_nodes", ["node_type"])
    op.create_index(
        "ix_workflow_edges_workflow_id", "workflow_edges", ["workflow_id"]
    )
    op.create_index("ix_workflow_edges_source_id", "workflow_edges", ["source_id"])
    op.create_index("ix_workflow_edges_target_id", "workflow_edges", ["target_id"])
    op.create_index(
        "ix_workflow_executions_workflow_id", "workflow_executions", ["workflow_id"]
    )
    op.create_index(
        "ix_workflow_executions_status", "workflow_executions", ["status"]
    )
    op.create_index(
        "ix_node_executions_execution_id", "node_executions", ["execution_id"]
    )
    op.create_index("ix_node_executions_node_id", "node_executions", ["node_id"])


def downgrade() -> None:
    for index, table in [
        ("ix_node_executions_node_id", "node_executions"),
        ("ix_node_executions_execution_id", "node_executions"),
        ("ix_workflow_executions_status", "workflow_executions"),
        ("ix_workflow_executions_workflow_id", "workflow_executions"),
        ("ix_workflow_edges_target_id", "workflow_edges"),
        ("ix_workflow_edges_source_id", "workflow_edges"),
        ("ix_workflow_edges_workflow_id", "workflow_edges"),
        ("ix_workflow_nodes_node_type", "workflow_nodes"),
        ("ix_workflow_nodes_workflow_id", "workflow_nodes"),
    ]:
        op.drop_index(index, table_name=table)

    with op.batch_alter_table("node_executions") as batch:
        batch.drop_column("duration_ms")
    with op.batch_alter_table("workflow_executions") as batch:
        batch.drop_column("finished_at")
        batch.drop_column("started_at")
        batch.drop_column("trigger")
    with op.batch_alter_table("workflow_edges") as batch:
        batch.drop_column("label")
    with op.batch_alter_table("workflow_nodes") as batch:
        batch.drop_column("retry_policy")
        batch.drop_column("position_y")
        batch.drop_column("position_x")
