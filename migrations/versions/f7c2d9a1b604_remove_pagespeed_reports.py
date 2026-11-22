"""remove stored PageSpeed reports

Revision ID: f7c2d9a1b604
Revises: e2a7c5b91d46
Create Date: 2026-11-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7c2d9a1b604"
down_revision: Union[str, Sequence[str], None] = "e2a7c5b91d46"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("pagespeed_scans", "report")


def downgrade() -> None:
    op.add_column(
        "pagespeed_scans",
        sa.Column(
            "report",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
