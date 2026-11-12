"""track when PageSpeed scan jobs start

Revision ID: 93b7d2f4a610
Revises: 82a4c6e19f30
Create Date: 2026-11-12 00:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "93b7d2f4a610"
down_revision: Union[str, Sequence[str], None] = "82a4c6e19f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pagespeed_scan_jobs",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pagespeed_scan_jobs", "started_at")
