"""allow PageSpeed scans to belong to websites

Revision ID: d05c8f19a2b7
Revises: c62e8f4a1b37
Create Date: 2026-11-19 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d05c8f19a2b7"
down_revision: Union[str, Sequence[str], None] = "c62e8f4a1b37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "pagespeed_scans",
        "company_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.add_column(
        "pagespeed_scans",
        sa.Column("website_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_pagespeed_scans_website_id_websites",
        "pagespeed_scans",
        "websites",
        ["website_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_pagespeed_scans_has_owner",
        "pagespeed_scans",
        "company_id IS NOT NULL OR website_id IS NOT NULL",
    )
    op.create_index(
        "ix_pagespeed_scans_website_scanned_at",
        "pagespeed_scans",
        ["website_id", "scanned_at"],
    )

    op.alter_column(
        "pagespeed_scan_jobs",
        "company_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.add_column(
        "pagespeed_scan_jobs",
        sa.Column("website_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_pagespeed_scan_jobs_website_id_websites",
        "pagespeed_scan_jobs",
        "websites",
        ["website_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_pagespeed_scan_jobs_has_owner",
        "pagespeed_scan_jobs",
        "company_id IS NOT NULL OR website_id IS NOT NULL",
    )
    op.create_index(
        "ix_pagespeed_scan_jobs_website_created_at",
        "pagespeed_scan_jobs",
        ["website_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in ("pagespeed_scan_jobs", "pagespeed_scans"):
        has_website_rows = bind.execute(
            sa.text(
                f"SELECT 1 FROM {table_name} "
                "WHERE website_id IS NOT NULL LIMIT 1"
            )
        ).first()
        if has_website_rows:
            raise RuntimeError(
                "Cannot downgrade while website-owned PageSpeed records exist."
            )

    op.drop_index(
        "ix_pagespeed_scan_jobs_website_created_at",
        table_name="pagespeed_scan_jobs",
    )
    op.drop_constraint(
        "ck_pagespeed_scan_jobs_has_owner",
        "pagespeed_scan_jobs",
        type_="check",
    )
    op.drop_constraint(
        "fk_pagespeed_scan_jobs_website_id_websites",
        "pagespeed_scan_jobs",
        type_="foreignkey",
    )
    op.drop_column("pagespeed_scan_jobs", "website_id")
    op.alter_column(
        "pagespeed_scan_jobs",
        "company_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )

    op.drop_index(
        "ix_pagespeed_scans_website_scanned_at",
        table_name="pagespeed_scans",
    )
    op.drop_constraint(
        "ck_pagespeed_scans_has_owner",
        "pagespeed_scans",
        type_="check",
    )
    op.drop_constraint(
        "fk_pagespeed_scans_website_id_websites",
        "pagespeed_scans",
        type_="foreignkey",
    )
    op.drop_column("pagespeed_scans", "website_id")
    op.alter_column(
        "pagespeed_scans",
        "company_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
