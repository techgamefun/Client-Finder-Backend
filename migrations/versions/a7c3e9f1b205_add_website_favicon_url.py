"""add website favicon URL

Revision ID: a7c3e9f1b205
Revises: e6f4a1b3c9d2
Create Date: 2026-11-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c3e9f1b205"
down_revision: Union[str, Sequence[str], None] = "e6f4a1b3c9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "websites",
        sa.Column("favicon_url", sa.String(length=2000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("websites", "favicon_url")
