"""create websites registry

Revision ID: e91a4f6b2c30
Revises: d4a9f1c7b205
Create Date: 2026-11-12 22:30:00.000000
"""

from typing import Sequence, Union
from uuid import UUID, uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "e91a4f6b2c30"
down_revision: Union[str, Sequence[str], None] = "d4a9f1c7b205"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "websites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("website_url", sa.String(length=2000), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=True),
        sa.Column("crawl_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column(
            "site_type",
            sa.String(length=16),
            server_default="normal",
            nullable=False,
        ),
        sa.Column(
            "discovery_source",
            sa.String(length=255),
            server_default="manual",
            nullable=False,
        ),
        sa.Column("discovery_provider", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "site_type IN ('normal', 'listing')",
            name="ck_websites_site_type",
        ),
        sa.CheckConstraint(
            "site_type != 'listing' OR company_id IS NULL",
            name="ck_listing_websites_unlinked",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["crawl_id"],
            ["website_crawls.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("website_url", name="uq_websites_website_url"),
        sa.UniqueConstraint("crawl_id", name="uq_websites_crawl_id"),
    )
    op.create_index("ix_websites_company_id", "websites", ["company_id"])
    op.create_index("ix_websites_site_type", "websites", ["site_type"])
    op.create_index("ix_websites_created_at", "websites", ["created_at"])

    bind = op.get_bind()
    websites = sa.table(
        "websites",
        sa.column("id", sa.Uuid()),
        sa.column("website_url", sa.String(2000)),
        sa.column("company_id", sa.Uuid()),
        sa.column("crawl_id", sa.Uuid()),
        sa.column("category", sa.String(64)),
        sa.column("site_type", sa.String(16)),
        sa.column("discovery_source", sa.String(255)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    crawl_rows = sa.table(
        "website_crawls",
        sa.column("id", sa.Uuid()),
        sa.column("website_url", sa.String(2000)),
        sa.column("site_type", sa.String(64)),
        sa.column("crawled_at", sa.DateTime(timezone=True)),
    )
    existing: dict[str, dict[str, object]] = {}

    for row in bind.execute(
        sa.select(
            crawl_rows.c.id,
            crawl_rows.c.website_url,
            crawl_rows.c.site_type,
            crawl_rows.c.crawled_at,
        )
    ):
        is_listing = row.site_type == "directories_listings"
        timestamp = row.crawled_at
        values = {
            "id": row.id,
            "website_url": row.website_url,
            "company_id": None,
            "crawl_id": row.id,
            "category": row.site_type,
            "site_type": "listing" if is_listing else "normal",
            "discovery_source": (
                "listing_website" if is_listing else "existing_crawl"
            ),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        bind.execute(sa.insert(websites).values(**values))
        existing[row.website_url] = values

    companies = sa.table(
        "companies",
        sa.column("id", sa.Uuid()),
        sa.column("website", sa.String(500)),
        sa.column("domain", sa.String(255)),
    )
    for row in bind.execute(
        sa.select(companies.c.id, companies.c.website, companies.c.domain)
    ):
        website_url = row.website or (
            f"https://{row.domain}" if row.domain else None
        )
        if website_url is None:
            continue
        if website_url in existing:
            website = existing[website_url]
            if website["site_type"] != "listing" and website["company_id"] is None:
                website["company_id"] = row.id
                bind.execute(
                    sa.update(websites)
                    .where(websites.c.website_url == website_url)
                    .values(company_id=row.id)
                )
            continue
        values = {
            "id": _new_uuid(),
            "website_url": website_url,
            "company_id": row.id,
            "crawl_id": None,
            "category": None,
            "site_type": "normal",
            "discovery_source": "company_profile",
            "created_at": None,
            "updated_at": None,
        }
        bind.execute(
            sa.insert(websites).values(
                **{key: value for key, value in values.items() if value is not None}
            )
        )
        existing[website_url] = values

    sources = sa.table(
        "lead_sources",
        sa.column("company_id", sa.Uuid()),
        sa.column("source_url", sa.String(2000)),
    )
    for row in bind.execute(
        sa.select(sources.c.company_id, sources.c.source_url)
    ):
        if row.source_url in existing:
            website = existing[row.source_url]
            if website["site_type"] != "listing" and website["company_id"] is None:
                website["company_id"] = row.company_id
                bind.execute(
                    sa.update(websites)
                    .where(websites.c.website_url == row.source_url)
                    .values(company_id=row.company_id)
                )
            continue
        values = {
            "id": _new_uuid(),
            "website_url": row.source_url,
            "company_id": row.company_id,
            "crawl_id": None,
            "category": None,
            "site_type": "normal",
            "discovery_source": "existing_data",
            "created_at": None,
            "updated_at": None,
        }
        bind.execute(
            sa.insert(websites).values(
                **{key: value for key, value in values.items() if value is not None}
            )
        )
        existing[row.source_url] = values

    jobs = sa.table(
        "website_crawl_jobs",
        sa.column("website_url", sa.String(2000)),
    )
    for row in bind.execute(sa.select(jobs.c.website_url)):
        if row.website_url in existing:
            continue
        bind.execute(
            sa.insert(websites).values(
                id=_new_uuid(),
                website_url=row.website_url,
                company_id=None,
                crawl_id=None,
                category=None,
                site_type="normal",
                discovery_source="manual",
            )
        )
        existing[row.website_url] = {"site_type": "normal"}


def downgrade() -> None:
    op.drop_index("ix_websites_created_at", table_name="websites")
    op.drop_index("ix_websites_site_type", table_name="websites")
    op.drop_index("ix_websites_company_id", table_name="websites")
    op.drop_table("websites")


def _new_uuid() -> UUID:
    return uuid4()
