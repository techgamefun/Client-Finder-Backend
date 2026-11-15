"""persist repeated website corpus words

Revision ID: c2f71d8a4b60
Revises: d7c58a1e4f20
Create Date: 2026-11-14 12:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2f71d8a4b60"
down_revision: Union[str, Sequence[str], None] = "d7c58a1e4f20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "website_corpus_analysis",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "words",
            sa.JSON(),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "website_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("source_crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "id = 1",
            name="ck_website_corpus_analysis_singleton",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("website_corpus_analysis")
