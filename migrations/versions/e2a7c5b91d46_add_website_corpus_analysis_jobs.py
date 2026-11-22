"""queue per-website corpus keyword analysis

Revision ID: e2a7c5b91d46
Revises: d1a5b7c9e2f4
Create Date: 2026-11-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2a7c5b91d46"
down_revision: Union[str, Sequence[str], None] = "d1a5b7c9e2f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "website_corpus_analysis_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.Column("source_crawled_at", sa.DateTime(timezone=True), nullable=False),
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
            name="ck_website_corpus_analysis_jobs_status_allowed",
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
        sa.UniqueConstraint(
            "website_id",
            "source_crawled_at",
            name="uq_website_corpus_analysis_jobs_website_crawl",
        ),
    )
    op.create_index(
        "ix_website_corpus_analysis_jobs_status_created_at",
        "website_corpus_analysis_jobs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_website_corpus_analysis_jobs_organization_created_at",
        "website_corpus_analysis_jobs",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_website_corpus_analysis_jobs_website_created_at",
        "website_corpus_analysis_jobs",
        ["website_id", "created_at"],
    )
    op.create_index(
        op.f("ix_website_corpus_analysis_jobs_organization_id"),
        "website_corpus_analysis_jobs",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_website_corpus_analysis_jobs_created_by_user_id"),
        "website_corpus_analysis_jobs",
        ["created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_website_corpus_analysis_jobs_created_by_user_id"),
        table_name="website_corpus_analysis_jobs",
    )
    op.drop_index(
        op.f("ix_website_corpus_analysis_jobs_organization_id"),
        table_name="website_corpus_analysis_jobs",
    )
    op.drop_index(
        "ix_website_corpus_analysis_jobs_website_created_at",
        table_name="website_corpus_analysis_jobs",
    )
    op.drop_index(
        "ix_website_corpus_analysis_jobs_organization_created_at",
        table_name="website_corpus_analysis_jobs",
    )
    op.drop_index(
        "ix_website_corpus_analysis_jobs_status_created_at",
        table_name="website_corpus_analysis_jobs",
    )
    op.drop_table("website_corpus_analysis_jobs")
