"""add organization Tavily search preferences

Revision ID: b2d4f6a8c103
Revises: a7c3e9f1b205
Create Date: 2026-11-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2d4f6a8c103"
down_revision: Union[str, Sequence[str], None] = "a7c3e9f1b205"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "application_settings",
        sa.Column(
            "tavily_default_search_depth",
            sa.String(length=20),
            server_default="basic",
            nullable=False,
        ),
    )
    op.add_column(
        "application_settings",
        sa.Column("tavily_default_country", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "application_settings",
        sa.Column(
            "tavily_default_exclude_domains",
            sa.JSON(),
            server_default="[]",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("application_settings", "tavily_default_exclude_domains")
    op.drop_column("application_settings", "tavily_default_country")
    op.drop_column("application_settings", "tavily_default_search_depth")
