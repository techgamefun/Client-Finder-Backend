"""add tenant-scoped excluded domains and crawl failure tracking

Revision ID: f1a2b3c4d5e6
Revises: d05c8f19a2b7
Create Date: 2026-11-20 00:00:00.000000
"""

from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "d05c8f19a2b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PRESET_DOMAINS = (
    "google.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
    "reddit.com",
    "wikipedia.org",
    "amazon.com",
    "apple.com",
    "microsoft.com",
)


def upgrade() -> None:
    op.add_column(
        "website_crawl_jobs",
        sa.Column("skip_reason", sa.Text(), nullable=True),
    )
    op.create_table(
        "excluded_domains",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sa.String(length=24),
            server_default="user",
            nullable=False,
        ),
        sa.Column(
            "failure_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('Big Tech / Social', 'Manual Exclusion', "
            "'Unreachable', 'Blocked (403/429)')",
            name="ck_excluded_domains_reason_allowed",
        ),
        sa.CheckConstraint(
            "source IN ('system_preset', 'user', 'auto_crawler')",
            name="ck_excluded_domains_source_allowed",
        ),
        sa.CheckConstraint(
            "failure_count >= 0",
            name="ck_excluded_domains_failure_count_nonnegative",
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
        sa.UniqueConstraint(
            "organization_id",
            "domain",
            name="uq_excluded_domains_organization_domain",
        ),
    )
    op.create_index(
        "ix_excluded_domains_organization_id",
        "excluded_domains",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "crawler_domain_failures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column(
            "failure_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
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
        sa.UniqueConstraint(
            "organization_id",
            "domain",
            name="uq_crawler_domain_failures_organization_domain",
        ),
    )
    op.create_index(
        "ix_crawler_domain_failures_organization_id",
        "crawler_domain_failures",
        ["organization_id"],
        unique=False,
    )

    connection = op.get_bind()
    organizations = sa.table("organizations", sa.column("id", sa.Uuid()))
    excluded_domains = sa.table(
        "excluded_domains",
        sa.column("id", sa.Uuid()),
        sa.column("organization_id", sa.Uuid()),
        sa.column("domain", sa.String(length=255)),
        sa.column("reason", sa.String(length=40)),
        sa.column("source", sa.String(length=24)),
        sa.column("failure_count", sa.Integer()),
    )
    organization_ids = connection.execute(
        sa.select(organizations.c.id)
    ).scalars()
    rows = [
        {
            "id": uuid4(),
            "organization_id": organization_id,
            "domain": domain,
            "reason": "Big Tech / Social",
            "source": "system_preset",
            "failure_count": 0,
        }
        for organization_id in organization_ids
        for domain in PRESET_DOMAINS
    ]
    if rows:
        op.bulk_insert(excluded_domains, rows)


def downgrade() -> None:
    op.drop_index(
        "ix_crawler_domain_failures_organization_id",
        table_name="crawler_domain_failures",
    )
    op.drop_table("crawler_domain_failures")
    op.drop_index(
        "ix_excluded_domains_organization_id",
        table_name="excluded_domains",
    )
    op.drop_table("excluded_domains")
    op.drop_column("website_crawl_jobs", "skip_reason")
