"""add durable website crawl jobs

Revision ID: bd2f4a8c91e7
Revises: e83b9fd41c26
Create Date: 2026-11-10 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bd2f4a8c91e7"
down_revision: Union[str, Sequence[str], None] = "e83b9fd41c26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "website_crawl_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("website_url", sa.String(length=2000), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("crawl_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["crawl_id"],
            ["website_crawls.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_website_crawl_jobs_status",
        "website_crawl_jobs",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_website_crawl_jobs_status", table_name="website_crawl_jobs")
    op.drop_table("website_crawl_jobs")
