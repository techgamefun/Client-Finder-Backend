"""add platform-wide email suppression records

Revision ID: c51a7d2e9f40
Revises: c4f9a2d7e631
Create Date: 2026-11-17 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c51a7d2e9f40"
down_revision: Union[str, Sequence[str], None] = "c4f9a2d7e631"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "suppressed_contacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("normalized_value", sa.String(length=320), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column(
            "global_dnc",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_type IN ('email', 'domain')",
            name="ck_suppressed_contacts_target_type",
        ),
        sa.CheckConstraint(
            "reason IN ('unsubscribe', 'hard_bounce', 'complaint', 'manual_dnc')",
            name="ck_suppressed_contacts_reason",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "target_type",
            "normalized_value",
            "reason",
            name="uq_suppressed_contacts_entry",
        ),
    )
    op.create_index(
        "ix_suppressed_contacts_lookup",
        "suppressed_contacts",
        ["organization_id", "target_type", "normalized_value"],
    )
    op.create_index(
        "ix_suppressed_contacts_global_lookup",
        "suppressed_contacts",
        ["global_dnc", "target_type", "normalized_value"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_suppressed_contacts_global_lookup",
        table_name="suppressed_contacts",
    )
    op.drop_index(
        "ix_suppressed_contacts_lookup",
        table_name="suppressed_contacts",
    )
    op.drop_table("suppressed_contacts")
