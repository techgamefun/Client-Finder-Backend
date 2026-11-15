"""persist phone numbers extracted by website crawls

Revision ID: f68b2a1c9d43
Revises: e8f4c2b9a761
Create Date: 2026-11-16 03:55:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f68b2a1c9d43"
down_revision: Union[str, Sequence[str], None] = "e8f4c2b9a761"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "website_crawls",
        sa.Column(
            "phones",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("website_crawls", "phones")
