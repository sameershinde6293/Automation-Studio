"""V1.1 M5: identity tables and the missing audit_events migration

Revision ID: d5f3a7c81b64
Revises: c4e7a1b90d52
Create Date: 2026-07-26

Two things happen here.

1. **``audit_events`` finally gets a migration.** The table has existed as an
   ORM model since V1.0 but was only ever created by ``Base.metadata.create_all``.
   Any deployment that migrated with Alembic alone (the documented production
   path) started without the table, so audit writes failed at runtime. This was
   logged as known issue #10 in M4 and is fixed here. The create is guarded by
   an inspector check so instances that already have the table via
   ``create_all`` upgrade cleanly instead of erroring on "table exists".

2. **Identity tables are added** to support M5 authentication:
   ``users``, ``api_keys`` and ``refresh_sessions``.

No existing column is altered and no data is rewritten, so the upgrade is safe
on a populated database and the downgrade is exact.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5f3a7c81b64"
down_revision: Union[str, Sequence[str], None] = "c4e7a1b90d52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    # ---------------------------------------------------------------- #
    # 1. audit_events (pre-existing model, previously missing a migration)
    # ---------------------------------------------------------------- #
    if not _has_table("audit_events"):
        op.create_table(
            "audit_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("event_name", sa.String(), nullable=False),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_audit_events_id", "audit_events", ["id"])
        op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])

    # An audit log is queried by actor and by event name; both were unindexed.
    existing_audit_indexes = set()
    if _has_table("audit_events"):
        bind = op.get_bind()
        existing_audit_indexes = {
            idx["name"] for idx in sa.inspect(bind).get_indexes("audit_events")
        }
    if "ix_audit_events_user_id" not in existing_audit_indexes:
        op.create_index("ix_audit_events_user_id", "audit_events", ["user_id"])
    if "ix_audit_events_event_name" not in existing_audit_indexes:
        op.create_index("ix_audit_events_event_name", "audit_events", ["event_name"])

    # ---------------------------------------------------------------- #
    # 2. Identity
    # ---------------------------------------------------------------- #
    if not _has_table("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("username", sa.String(length=150), nullable=False),
            sa.Column("email", sa.String(length=320), nullable=True),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column(
                "role", sa.String(length=50), nullable=False, server_default="viewer"
            ),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("last_login_at", sa.DateTime(), nullable=True),
            sa.Column(
                "failed_login_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("locked_until", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("username", name="uq_users_username"),
            sa.UniqueConstraint("email", name="uq_users_email"),
        )
        op.create_index("ix_users_id", "users", ["id"])
        op.create_index("ix_users_username", "users", ["username"])
        op.create_index("ix_users_email", "users", ["email"])
        op.create_index("ix_users_role", "users", ["role"])
        op.create_index("ix_users_is_active", "users", ["is_active"])
        op.create_index("ix_users_created_at", "users", ["created_at"])

    if not _has_table("api_keys"):
        op.create_table(
            "api_keys",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=150), nullable=False),
            sa.Column("key_hash", sa.String(length=64), nullable=False),
            sa.Column("prefix", sa.String(length=16), nullable=False),
            sa.Column(
                "scopes", sa.String(length=500), nullable=False, server_default=""
            ),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
        )
        op.create_index("ix_api_keys_id", "api_keys", ["id"])
        op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
        op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"])
        op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"])
        op.create_index("ix_api_keys_is_active", "api_keys", ["is_active"])
        op.create_index("ix_api_keys_expires_at", "api_keys", ["expires_at"])
        op.create_index("ix_api_keys_created_at", "api_keys", ["created_at"])
        op.create_index("ix_api_keys_user_active", "api_keys", ["user_id", "is_active"])

    if not _has_table("refresh_sessions"):
        op.create_table(
            "refresh_sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("user_agent", sa.String(length=300), nullable=True),
            sa.Column("client_ip", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint("token_hash", name="uq_refresh_sessions_token_hash"),
        )
        op.create_index("ix_refresh_sessions_id", "refresh_sessions", ["id"])
        op.create_index("ix_refresh_sessions_user_id", "refresh_sessions", ["user_id"])
        op.create_index(
            "ix_refresh_sessions_token_hash", "refresh_sessions", ["token_hash"]
        )
        op.create_index(
            "ix_refresh_sessions_expires_at", "refresh_sessions", ["expires_at"]
        )
        op.create_index(
            "ix_refresh_sessions_revoked_at", "refresh_sessions", ["revoked_at"]
        )
        op.create_index(
            "ix_refresh_sessions_created_at", "refresh_sessions", ["created_at"]
        )
        op.create_index(
            "ix_refresh_sessions_user_revoked",
            "refresh_sessions",
            ["user_id", "revoked_at"],
        )


def downgrade() -> None:
    # Identity tables are M5-only, so the downgrade removes them entirely.
    for table in ("refresh_sessions", "api_keys", "users"):
        if _has_table(table):
            op.drop_table(table)

    # ``audit_events`` predates this migration as a model, so the downgrade
    # only removes the indexes this revision added. Dropping the table would
    # destroy audit history that existed before M5.
    if _has_table("audit_events"):
        bind = op.get_bind()
        existing = {idx["name"] for idx in sa.inspect(bind).get_indexes("audit_events")}
        for index_name in ("ix_audit_events_event_name", "ix_audit_events_user_id"):
            if index_name in existing:
                op.drop_index(index_name, table_name="audit_events")
