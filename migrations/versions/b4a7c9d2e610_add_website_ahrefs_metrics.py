"""cache Ahrefs metrics per website and user

Revision ID: b4a7c9d2e610
Revises: f8b4c6d2a109
Create Date: 2026-11-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4a7c9d2e610"
down_revision: Union[str, Sequence[str], None] = "f8b4c6d2a109"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "website_ahrefs_metrics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("domain_rating", sa.Float(), nullable=False),
        sa.Column(
            "domain_rating_license_url",
            sa.String(length=2048),
            nullable=False,
        ),
        sa.Column("organic_traffic", sa.Integer(), nullable=True),
        sa.Column("organic_traffic_cost_cents", sa.Integer(), nullable=True),
        sa.Column("organic_keywords", sa.Integer(), nullable=True),
        sa.Column("backlinks_live", sa.Integer(), nullable=True),
        sa.Column("backlinks_all_time", sa.Integer(), nullable=True),
        sa.Column("refdomains_live", sa.Integer(), nullable=True),
        sa.Column("refdomains_all_time", sa.Integer(), nullable=True),
        sa.Column("units_used", sa.Integer(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_website_ahrefs_metrics_created_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_website_ahrefs_metrics_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name="fk_website_ahrefs_metrics_website_id_websites",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_website_ahrefs_metrics"),
        sa.UniqueConstraint(
            "website_id",
            "created_by_user_id",
            name="uq_website_ahrefs_metrics_website_user",
        ),
    )
    op.create_index(
        "ix_website_ahrefs_metrics_organization_id",
        "website_ahrefs_metrics",
        ["organization_id"],
    )
    op.create_index(
        "ix_website_ahrefs_metrics_created_by_user_id",
        "website_ahrefs_metrics",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_website_ahrefs_metrics_user_fetched_at",
        "website_ahrefs_metrics",
        ["created_by_user_id", "fetched_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_website_ahrefs_metrics_user_fetched_at",
        table_name="website_ahrefs_metrics",
    )
    op.drop_index(
        "ix_website_ahrefs_metrics_created_by_user_id",
        table_name="website_ahrefs_metrics",
    )
    op.drop_index(
        "ix_website_ahrefs_metrics_organization_id",
        table_name="website_ahrefs_metrics",
    )
    op.drop_table("website_ahrefs_metrics")
