"""add indexes for overview aggregates

Revision ID: c4f9a2d7e631
Revises: b2d4f6a8c103
Create Date: 2026-11-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "c4f9a2d7e631"
down_revision: Union[str, Sequence[str], None] = "b2d4f6a8c103"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_contacts_company_organization",
        "contacts",
        ["company_id", "organization_id"],
    )
    op.create_index(
        "ix_companies_organization_created_at",
        "companies",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_websites_organization_created_at",
        "websites",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_email_campaign_recipients_org_status_sent_at",
        "email_campaign_recipients",
        ["org_id", "status", "sent_at"],
    )
    op.create_index(
        "ix_website_crawl_jobs_organization_created_at",
        "website_crawl_jobs",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_website_crawl_jobs_organization_completed_at",
        "website_crawl_jobs",
        ["organization_id", "completed_at"],
    )
    op.create_index(
        "ix_pagespeed_scan_jobs_organization_created_at",
        "pagespeed_scan_jobs",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pagespeed_scan_jobs_organization_created_at",
        table_name="pagespeed_scan_jobs",
    )
    op.drop_index(
        "ix_website_crawl_jobs_organization_completed_at",
        table_name="website_crawl_jobs",
    )
    op.drop_index(
        "ix_website_crawl_jobs_organization_created_at",
        table_name="website_crawl_jobs",
    )
    op.drop_index(
        "ix_email_campaign_recipients_org_status_sent_at",
        table_name="email_campaign_recipients",
    )
    op.drop_index(
        "ix_websites_organization_created_at",
        table_name="websites",
    )
    op.drop_index(
        "ix_companies_organization_created_at",
        table_name="companies",
    )
    op.drop_index(
        "ix_contacts_company_organization",
        table_name="contacts",
    )
