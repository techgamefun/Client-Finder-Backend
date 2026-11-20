"""add organization description and user names

Revision ID: e01e56fbc2a4
Revises: c84b6a21d9f3
Create Date: 2026-11-20 20:15:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e01e56fbc2a4"
down_revision: Union[str, Sequence[str], None] = "c84b6a21d9f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("description", sa.String(length=1000), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("first_name", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("last_name", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "last_name")
    op.drop_column("users", "first_name")
    op.drop_column("organizations", "description")
