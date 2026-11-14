"""store detailed website classification signals

Revision ID: c274e8f91b62
Revises: c19a7b3d5e21
Create Date: 2026-11-13 17:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c274e8f91b62"
down_revision: Union[str, Sequence[str], None] = "c19a7b3d5e21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "website_crawls",
        sa.Column(
            "classification_signals",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("website_crawls", "classification_signals")
