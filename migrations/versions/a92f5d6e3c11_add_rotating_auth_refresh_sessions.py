"""add rotating authentication refresh sessions

Revision ID: a92f5d6e3c11
Revises: f5a6b7c8d9e0
Create Date: 2026-11-15 18:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a92f5d6e3c11"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auth_refresh_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_auth_refresh_sessions_user_id",
        "auth_refresh_sessions",
        ["user_id"],
    )
    op.create_index(
        "ix_auth_refresh_sessions_expires_at",
        "auth_refresh_sessions",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auth_refresh_sessions_expires_at",
        table_name="auth_refresh_sessions",
    )
    op.drop_index(
        "ix_auth_refresh_sessions_user_id",
        table_name="auth_refresh_sessions",
    )
    op.drop_table("auth_refresh_sessions")
