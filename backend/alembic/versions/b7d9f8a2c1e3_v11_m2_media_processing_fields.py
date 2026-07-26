"""V1.1 M2 media processing fields

Revision ID: b7d9f8a2c1e3
Revises: a1b2c3d4e5f6
Create Date: 2026-07-26 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7d9f8a2c1e3"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("media_assets") as batch:
        batch.add_column(sa.Column("content_type", sa.String(), nullable=True))
        batch.add_column(sa.Column("size_bytes", sa.Integer(), nullable=True, server_default="0"))
        batch.add_column(sa.Column("checksum_sha256", sa.String(), nullable=True))
    op.create_index(op.f("ix_media_assets_checksum_sha256"), "media_assets", ["checksum_sha256"], unique=False)

    with op.batch_alter_table("media_processing_jobs") as batch:
        batch.add_column(sa.Column("progress", sa.Integer(), nullable=True, server_default="0"))
        batch.add_column(sa.Column("result", sa.JSON(), nullable=True))
        batch.create_foreign_key("fk_media_processing_jobs_asset_id", "media_assets", ["asset_id"], ["id"])
    op.create_index(op.f("ix_media_processing_jobs_status"), "media_processing_jobs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_media_processing_jobs_status"), table_name="media_processing_jobs")
    with op.batch_alter_table("media_processing_jobs") as batch:
        batch.drop_constraint("fk_media_processing_jobs_asset_id", type_="foreignkey")
        batch.drop_column("result")
        batch.drop_column("progress")
    op.drop_index(op.f("ix_media_assets_checksum_sha256"), table_name="media_assets")
    with op.batch_alter_table("media_assets") as batch:
        batch.drop_column("checksum_sha256")
        batch.drop_column("size_bytes")
        batch.drop_column("content_type")
