"""add user and organization images

Revision ID: c84b6a21d9f3
Revises: f1a2b3c4d5e6
Create Date: 2026-11-20 19:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c84b6a21d9f3"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("profile_photo_url", sa.String(length=2048), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("logo_url", sa.String(length=2048), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("banner_url", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "banner_url")
    op.drop_column("organizations", "logo_url")
    op.drop_column("users", "profile_photo_url")
