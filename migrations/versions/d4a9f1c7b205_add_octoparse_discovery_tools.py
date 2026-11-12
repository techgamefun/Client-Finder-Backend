"""add Octoparse discovery tools

Revision ID: d4a9f1c7b205
Revises: 3a4f6c91d2e8
Create Date: 2026-11-12 16:56:24.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4a9f1c7b205"
down_revision: Union[str, Sequence[str], None] = "3a4f6c91d2e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "application_settings",
        sa.Column("octoparse_api_key_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "application_settings",
        sa.Column("selected_discovery_tools", sa.JSON(), nullable=True),
    )
    op.add_column(
        "application_settings",
        sa.Column("octoparse_template", sa.JSON(), nullable=True),
    )
    op.create_table(
        "discovery_provider_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("search_keyword_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_task_id", sa.String(length=255), nullable=False),
        sa.Column("lot_no", sa.String(length=255), nullable=True),
        sa.Column("max_results", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_mapping", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("warning", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["search_keyword_id"],
            ["search_keywords.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_discovery_provider_jobs_search_keyword_id",
        "discovery_provider_jobs",
        ["search_keyword_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_discovery_provider_jobs_search_keyword_id",
        table_name="discovery_provider_jobs",
    )
    op.drop_table("discovery_provider_jobs")
    op.drop_column("application_settings", "octoparse_template")
    op.drop_column("application_settings", "selected_discovery_tools")
    op.drop_column("application_settings", "octoparse_api_key_encrypted")
