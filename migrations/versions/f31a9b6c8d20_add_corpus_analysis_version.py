"""track corpus analyzer cache version

Revision ID: f31a9b6c8d20
Revises: c2f71d8a4b60
Create Date: 2026-11-14 18:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f31a9b6c8d20"
down_revision: Union[str, Sequence[str], None] = "c2f71d8a4b60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "website_corpus_analysis",
        sa.Column(
            "algorithm_version",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("website_corpus_analysis", "algorithm_version")
