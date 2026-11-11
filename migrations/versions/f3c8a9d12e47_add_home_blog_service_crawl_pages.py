"""add homepage, blog, and service website crawl pages

Revision ID: f3c8a9d12e47
Revises: bd2f4a8c91e7
Create Date: 2026-11-11 01:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3c8a9d12e47"
down_revision: Union[str, Sequence[str], None] = "bd2f4a8c91e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "website_crawls",
        sa.Column("home_page_url", sa.String(length=2000), nullable=True),
    )
    op.add_column(
        "website_crawls",
        sa.Column("home_content", sa.Text(), nullable=True),
    )
    op.add_column(
        "website_crawls",
        sa.Column("blog_page_url", sa.String(length=2000), nullable=True),
    )
    op.add_column(
        "website_crawls",
        sa.Column("blog_content", sa.Text(), nullable=True),
    )
    op.add_column(
        "website_crawls",
        sa.Column("service_page_url", sa.String(length=2000), nullable=True),
    )
    op.add_column(
        "website_crawls",
        sa.Column("service_content", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("website_crawls", "service_content")
    op.drop_column("website_crawls", "service_page_url")
    op.drop_column("website_crawls", "blog_content")
    op.drop_column("website_crawls", "blog_page_url")
    op.drop_column("website_crawls", "home_content")
    op.drop_column("website_crawls", "home_page_url")
