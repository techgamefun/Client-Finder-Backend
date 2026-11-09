"""add independent website crawl storage

Revision ID: 9a4f6c2d1b73
Revises: 7d91ca4f2e18
Create Date: 2026-11-08 23:50:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9a4f6c2d1b73"
down_revision: Union[str, Sequence[str], None] = "7d91ca4f2e18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "website_crawls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("website_url", sa.String(length=2000), nullable=False),
        sa.Column("about_page_url", sa.String(length=2000), nullable=True),
        sa.Column("about_content", sa.Text(), nullable=True),
        sa.Column("contact_page_url", sa.String(length=2000), nullable=True),
        sa.Column("contact_content", sa.Text(), nullable=True),
        sa.Column("goals_page_url", sa.String(length=2000), nullable=True),
        sa.Column("goals_content", sa.Text(), nullable=True),
        sa.Column("emails", sa.JSON(), nullable=False),
        sa.Column("social_links", sa.JSON(), nullable=False),
        sa.Column(
            "crawled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("website_url"),
    )


def downgrade() -> None:
    op.drop_table("website_crawls")
