"""add website workflow statuses

Revision ID: e6f4a9d2c731
Revises: d34f9a2c6e10
Create Date: 2026-11-28 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6f4a9d2c731"
down_revision: Union[str, Sequence[str], None] = "d34f9a2c6e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("websites") as batch_op:
        batch_op.drop_constraint("ck_websites_status_allowed", type_="check")
        batch_op.create_check_constraint(
            "ck_websites_status_allowed",
            "status IN ('new', 'lead', 'checked', 'qualified', 'contacted', "
            "'converted', 'disqualified', 'needs_review')",
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE websites SET status = 'new' "
            "WHERE status IN ('qualified', 'contacted', 'converted', "
            "'disqualified', 'needs_review')"
        )
    )
    with op.batch_alter_table("websites") as batch_op:
        batch_op.drop_constraint("ck_websites_status_allowed", type_="check")
        batch_op.create_check_constraint(
            "ck_websites_status_allowed",
            "status IN ('new', 'lead', 'checked')",
        )
