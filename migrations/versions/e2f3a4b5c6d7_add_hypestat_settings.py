"""add encrypted HypeStat settings

Revision ID: e2f3a4b5c6d7
Revises: d1e8f4a6b0c2
Create Date: 2026-11-15 17:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "d1e8f4a6b0c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "application_settings",
        sa.Column("hypestat_api_key_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "application_settings",
        sa.Column(
            "hypestat_auto_fetch_enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("application_settings", "hypestat_auto_fetch_enabled")
    op.drop_column("application_settings", "hypestat_api_key_encrypted")
