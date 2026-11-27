"""add personal workspace marker

Revision ID: d34f9a2c6e10
Revises: d24a11f9c830
Create Date: 2026-11-27 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d34f9a2c6e10"
down_revision: Union[str, Sequence[str], None] = "d24a11f9c830"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "is_personal",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("organizations", "is_personal")
