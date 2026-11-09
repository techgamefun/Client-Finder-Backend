"""track discovery search keywords and company associations

Revision ID: 5c81d2e7a4b9
Revises: 9a4f6c2d1b73
Create Date: 2026-11-09 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5c81d2e7a4b9"
down_revision: Union[str, Sequence[str], None] = "9a4f6c2d1b73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "search_keywords",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("keyword", sa.String(length=300), nullable=False),
        sa.Column("searched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_search_keywords_searched_at",
        "search_keywords",
        ["searched_at"],
        unique=False,
    )
    op.create_table(
        "company_search_keywords",
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("search_keyword_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["search_keyword_id"],
            ["search_keywords.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("company_id", "search_keyword_id"),
    )


def downgrade() -> None:
    op.drop_table("company_search_keywords")
    op.drop_index(
        "ix_search_keywords_searched_at",
        table_name="search_keywords",
    )
    op.drop_table("search_keywords")
