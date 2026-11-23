"""add lead batches for website imports

Revision ID: 9a7c3e1f5b20
Revises: f9a4c2d1e706
Create Date: 2026-11-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9a7c3e1f5b20"
down_revision: Union[str, Sequence[str], None] = "f9a4c2d1e706"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lead_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_lead_batches_created_by_user_id",
        "lead_batches",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_lead_batches_organization_id",
        "lead_batches",
        ["organization_id"],
    )
    op.create_index(
        "ix_lead_batches_organization_created_at",
        "lead_batches",
        ["organization_id", "created_at"],
    )
    op.create_table(
        "lead_batch_companies",
        sa.Column("lead_batch_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_batch_id"],
            ["lead_batches.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "lead_batch_id",
            "company_id",
            name="pk_lead_batch_companies",
        ),
    )
    op.create_index(
        "ix_lead_batch_companies_company_id",
        "lead_batch_companies",
        ["company_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_lead_batch_companies_company_id",
        table_name="lead_batch_companies",
    )
    op.drop_table("lead_batch_companies")
    op.drop_index(
        "ix_lead_batches_organization_created_at",
        table_name="lead_batches",
    )
    op.drop_index(
        "ix_lead_batches_organization_id",
        table_name="lead_batches",
    )
    op.drop_index(
        "ix_lead_batches_created_by_user_id",
        table_name="lead_batches",
    )
    op.drop_table("lead_batches")
