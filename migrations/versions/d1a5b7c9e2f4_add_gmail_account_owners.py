"""add owners to personal Gmail accounts

Revision ID: d1a5b7c9e2f4
Revises: c5d7e9a1b3f2
Create Date: 2026-11-22 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1a5b7c9e2f4"
down_revision: Union[str, Sequence[str], None] = "c5d7e9a1b3f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "connected_gmail_accounts",
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_connected_gmail_accounts_owner_user_id_users",
        "connected_gmail_accounts",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_connected_gmail_accounts_owner_user_id",
        "connected_gmail_accounts",
        ["owner_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connected_gmail_accounts_owner_user_id",
        table_name="connected_gmail_accounts",
    )
    op.drop_constraint(
        "fk_connected_gmail_accounts_owner_user_id_users",
        "connected_gmail_accounts",
        type_="foreignkey",
    )
    op.drop_column("connected_gmail_accounts", "owner_user_id")
