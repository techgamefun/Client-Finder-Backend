"""add connected Gmail accounts and user assignments

Revision ID: d8e6f4a2c901
Revises: b7e4c2a9d681
Create Date: 2026-11-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8e6f4a2c901"
down_revision: Union[str, Sequence[str], None] = "b7e4c2a9d681"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "connected_gmail_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("email_address", sa.String(length=320), nullable=False),
        sa.Column("google_account_id", sa.String(length=255), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("token_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_connected_gmail_accounts_org_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "google_account_id",
            name="uq_connected_gmail_google_account_id",
        ),
    )
    op.create_index(
        "ix_connected_gmail_accounts_org_id",
        "connected_gmail_accounts",
        ["org_id"],
    )
    op.create_table(
        "user_gmail_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("gmail_account_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by"],
            ["users.id"],
            name="fk_user_gmail_assigned_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["gmail_account_id"],
            ["connected_gmail_accounts.id"],
            name="fk_user_gmail_account_connected_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_user_gmail_org_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_gmail_user_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "gmail_account_id",
            name="uq_user_gmail_assignment",
        ),
    )
    op.create_index(
        "ix_user_gmail_assignments_org_id",
        "user_gmail_assignments",
        ["org_id"],
    )
    op.create_index(
        "ix_user_gmail_assignments_user_id",
        "user_gmail_assignments",
        ["user_id"],
    )
    op.create_index(
        "ix_user_gmail_assignments_gmail_account_id",
        "user_gmail_assignments",
        ["gmail_account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_gmail_assignments_gmail_account_id",
        table_name="user_gmail_assignments",
    )
    op.drop_index(
        "ix_user_gmail_assignments_user_id",
        table_name="user_gmail_assignments",
    )
    op.drop_index(
        "ix_user_gmail_assignments_org_id",
        table_name="user_gmail_assignments",
    )
    op.drop_table("user_gmail_assignments")
    op.drop_index(
        "ix_connected_gmail_accounts_org_id",
        table_name="connected_gmail_accounts",
    )
    op.drop_table("connected_gmail_accounts")
