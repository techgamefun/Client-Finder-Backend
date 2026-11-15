"""store HypeStat profiles and lookup status per website

Revision ID: d1e8f4a6b0c2
Revises: c9e8a7b6d5f4
Create Date: 2026-11-15 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e8f4a6b0c2"
down_revision: Union[str, Sequence[str], None] = "c9e8a7b6d5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "website_hypestat_profiles",
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'queued', 'failed', 'invalid_domain', 'removed')",
            name="ck_website_hypestat_profiles_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name="fk_website_hypestat_profiles_website_id_websites",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "website_id",
            name="pk_website_hypestat_profiles",
        ),
    )


def downgrade() -> None:
    op.drop_table("website_hypestat_profiles")
