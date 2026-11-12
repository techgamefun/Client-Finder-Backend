"""add secured application settings

Revision ID: 3a4f6c91d2e8
Revises: c83609f2e751
Create Date: 2026-11-12 16:20:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3a4f6c91d2e8"
down_revision: Union[str, Sequence[str], None] = "c83609f2e751"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "application_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tavily_api_key_encrypted", sa.Text(), nullable=True),
        sa.Column(
            "crawl_max_pages",
            sa.Integer(),
            server_default="6",
            nullable=False,
        ),
        sa.Column(
            "crawl_max_depth",
            sa.Integer(),
            server_default="2",
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("application_settings")
