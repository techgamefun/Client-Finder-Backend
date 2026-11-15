"""store corpus keywords separately for each website

Revision ID: a83c71e2b945
Revises: f31a9b6c8d20
Create Date: 2026-11-14 23:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a83c71e2b945"
down_revision: Union[str, Sequence[str], None] = "f31a9b6c8d20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("website_corpus_analysis")
    op.create_table(
        "website_corpus_analyses",
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.Column(
            "words",
            sa.JSON(),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "algorithm_version",
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
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name="fk_website_corpus_analyses_website_id_websites",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("website_id"),
    )


def downgrade() -> None:
    op.drop_table("website_corpus_analyses")
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
        sa.Column(
            "algorithm_version",
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
