"""remove the redundant website registry site type

Revision ID: f2a6d9c4b183
Revises: a83c71e2b945
Create Date: 2026-11-15 00:23:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2a6d9c4b183"
down_revision: Union[str, Sequence[str], None] = "a83c71e2b945"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE websites SET page_type = 'business_listing' "
            "WHERE site_type = 'listing'"
        )
    )
    op.drop_index("ix_websites_site_type", table_name="websites")
    with op.batch_alter_table("websites") as batch_op:
        batch_op.drop_constraint("ck_websites_site_type", type_="check")
        batch_op.drop_column("site_type")


def downgrade() -> None:
    with op.batch_alter_table("websites") as batch_op:
        batch_op.add_column(
            sa.Column(
                "site_type",
                sa.String(length=16),
                server_default="normal",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_websites_site_type",
            "site_type IN ('normal', 'listing')",
        )
    op.execute(
        sa.text(
            "UPDATE websites SET site_type = 'listing' "
            "WHERE page_type = 'business_listing'"
        )
    )
    op.create_index("ix_websites_site_type", "websites", ["site_type"])
