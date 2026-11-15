"""allow website registry lead review statuses

Revision ID: f0d7a4c29b61
Revises: f2a6d9c4b183
Create Date: 2026-11-15 00:37:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f0d7a4c29b61"
down_revision: Union[str, Sequence[str], None] = "f2a6d9c4b183"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("websites") as batch_op:
        batch_op.drop_constraint("ck_websites_status_new", type_="check")
        batch_op.create_check_constraint(
            "ck_websites_status_allowed",
            "status IN ('new', 'lead', 'checked')",
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE websites SET status = 'new' "
            "WHERE status IN ('lead', 'checked')"
        )
    )
    with op.batch_alter_table("websites") as batch_op:
        batch_op.drop_constraint("ck_websites_status_allowed", type_="check")
        batch_op.create_check_constraint(
            "ck_websites_status_new",
            "status = 'new'",
        )
