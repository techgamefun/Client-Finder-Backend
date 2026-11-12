"""link PageSpeed scan jobs to all associated companies

Revision ID: 82a4c6e19f30
Revises: 71e39c4a8d52
Create Date: 2026-11-12 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "82a4c6e19f30"
down_revision: Union[str, Sequence[str], None] = "71e39c4a8d52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pagespeed_scan_job_companies",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["pagespeed_scan_jobs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", "company_id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO pagespeed_scan_job_companies (job_id, company_id) "
            "SELECT id, company_id FROM pagespeed_scan_jobs"
        )
    )


def downgrade() -> None:
    op.drop_table("pagespeed_scan_job_companies")
