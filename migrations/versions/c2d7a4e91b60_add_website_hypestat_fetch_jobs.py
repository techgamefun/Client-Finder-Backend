"""queue background HypeStat fetches

Revision ID: c2d7a4e91b60
Revises: ab41c2d7e903
Create Date: 2026-11-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2d7a4e91b60"
down_revision: Union[str, Sequence[str], None] = "ab41c2d7e903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "website_hypestat_fetch_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'skipped')",
            name="ck_website_hypestat_fetch_jobs_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_website_hypestat_fetch_jobs_status_created_at",
        "website_hypestat_fetch_jobs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_website_hypestat_fetch_jobs_organization_created_at",
        "website_hypestat_fetch_jobs",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_website_hypestat_fetch_jobs_website_created_at",
        "website_hypestat_fetch_jobs",
        ["website_id", "created_at"],
    )
    op.create_index(
        op.f("ix_website_hypestat_fetch_jobs_organization_id"),
        "website_hypestat_fetch_jobs",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_website_hypestat_fetch_jobs_created_by_user_id"),
        "website_hypestat_fetch_jobs",
        ["created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_website_hypestat_fetch_jobs_created_by_user_id"),
        table_name="website_hypestat_fetch_jobs",
    )
    op.drop_index(
        op.f("ix_website_hypestat_fetch_jobs_organization_id"),
        table_name="website_hypestat_fetch_jobs",
    )
    op.drop_index(
        "ix_website_hypestat_fetch_jobs_website_created_at",
        table_name="website_hypestat_fetch_jobs",
    )
    op.drop_index(
        "ix_website_hypestat_fetch_jobs_organization_created_at",
        table_name="website_hypestat_fetch_jobs",
    )
    op.drop_index(
        "ix_website_hypestat_fetch_jobs_status_created_at",
        table_name="website_hypestat_fetch_jobs",
    )
    op.drop_table("website_hypestat_fetch_jobs")
