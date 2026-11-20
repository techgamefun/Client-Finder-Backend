"""add per-user preferences

Revision ID: 86b4d2e7c903
Revises: 75a9c2d0b13f
Create Date: 2026-11-20 22:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "86b4d2e7c903"
down_revision: Union[str, Sequence[str], None] = "75a9c2d0b13f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "preferences",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        "ix_user_preferences_organization_id",
        "user_preferences",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_preferences_organization_id",
        table_name="user_preferences",
    )
    op.drop_table("user_preferences")
