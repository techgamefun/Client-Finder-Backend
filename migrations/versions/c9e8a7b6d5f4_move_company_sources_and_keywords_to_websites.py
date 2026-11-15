"""move discovery keywords to websites and remove company research sources

Revision ID: c9e8a7b6d5f4
Revises: f0d7a4c29b61
Create Date: 2026-11-18 00:00:00.000000
"""

from typing import Sequence, Union
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "c9e8a7b6d5f4"
down_revision: Union[str, Sequence[str], None] = "f0d7a4c29b61"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _normalize_domain(value: str | None) -> str | None:
    if not value:
        return None

    value = value.strip()
    if "://" not in value:
        value = f"//{value}"
    try:
        hostname = urlsplit(value).hostname
    except ValueError:
        return None
    if not hostname:
        return None

    hostname = hostname.rstrip(".").lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def upgrade() -> None:
    bind = op.get_bind()
    companies = sa.table(
        "companies",
        sa.column("id", sa.Uuid()),
        sa.column("domain", sa.String(255)),
        sa.column("website", sa.String(500)),
    )
    websites = sa.table(
        "websites",
        sa.column("id", sa.Uuid()),
        sa.column("website_url", sa.String(2000)),
        sa.column("domain", sa.String(255)),
        sa.column("discovery_source", sa.String(255)),
    )
    company_keywords = sa.table(
        "company_search_keywords",
        sa.column("company_id", sa.Uuid()),
        sa.column("search_keyword_id", sa.Uuid()),
    )
    website_keywords = sa.table(
        "website_search_keywords",
        sa.column("website_id", sa.Uuid()),
        sa.column("search_keyword_id", sa.Uuid()),
    )

    company_domains: dict[UUID, str] = {}
    for row in bind.execute(
        sa.select(companies.c.id, companies.c.domain, companies.c.website)
    ):
        domain = _normalize_domain(row.domain) or _normalize_domain(row.website)
        if domain:
            company_domains[row.id] = domain

    websites_by_domain: dict[str, UUID] = {}
    for row in bind.execute(
        sa.select(websites.c.id, websites.c.domain)
    ):
        domain = _normalize_domain(row.domain)
        if domain:
            websites_by_domain[domain] = row.id

    keyword_company_ids = {
        row.company_id
        for row in bind.execute(
            sa.select(company_keywords.c.company_id).distinct()
        )
    }
    unmapped = [
        company_id
        for company_id in keyword_company_ids
        if company_id not in company_domains
    ]
    if unmapped:
        raise RuntimeError(
            "Cannot move company discovery keywords to websites: "
            f"{len(unmapped)} companies have no valid website domain."
        )

    op.create_table(
        "website_search_keywords",
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.Column("search_keyword_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["search_keyword_id"],
            ["search_keywords.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("website_id", "search_keyword_id"),
    )

    for company_id in keyword_company_ids:
        domain = company_domains[company_id]
        if domain in websites_by_domain:
            continue
        website_id = uuid4()
        bind.execute(
            sa.insert(websites).values(
                id=website_id,
                website_url=f"https://{domain}/",
                domain=domain,
                discovery_source="company_profile",
            )
        )
        websites_by_domain[domain] = website_id

    transferred_pairs: set[tuple[UUID, UUID]] = set()
    keyword_rows = bind.execute(
        sa.select(
            company_keywords.c.company_id,
            company_keywords.c.search_keyword_id,
        )
    )
    for row in keyword_rows:
        domain = company_domains[row.company_id]
        pair = (websites_by_domain[domain], row.search_keyword_id)
        if pair not in transferred_pairs:
            bind.execute(
                sa.insert(website_keywords).values(
                    website_id=pair[0],
                    search_keyword_id=pair[1],
                )
            )
            transferred_pairs.add(pair)

    op.drop_table("company_search_keywords")
    op.drop_table("lead_sources")


def downgrade() -> None:
    bind = op.get_bind()
    websites = sa.table(
        "websites",
        sa.column("id", sa.Uuid()),
        sa.column("domain", sa.String(255)),
    )
    companies = sa.table(
        "companies",
        sa.column("id", sa.Uuid()),
        sa.column("domain", sa.String(255)),
        sa.column("website", sa.String(500)),
    )
    website_keywords = sa.table(
        "website_search_keywords",
        sa.column("website_id", sa.Uuid()),
        sa.column("search_keyword_id", sa.Uuid()),
    )
    company_keywords = sa.table(
        "company_search_keywords",
        sa.column("company_id", sa.Uuid()),
        sa.column("search_keyword_id", sa.Uuid()),
    )

    company_by_domain: dict[str, UUID] = {}
    for row in bind.execute(
        sa.select(companies.c.id, companies.c.domain, companies.c.website)
    ):
        domain = _normalize_domain(row.domain) or _normalize_domain(row.website)
        if domain:
            company_by_domain[domain] = row.id

    website_domains: dict[UUID, str | None] = {
        row.id: _normalize_domain(row.domain)
        for row in bind.execute(
            sa.select(websites.c.id, websites.c.domain)
        )
    }
    associations = bind.execute(
        sa.select(
            website_keywords.c.website_id,
            website_keywords.c.search_keyword_id,
        )
    ).all()
    unmapped = [
        row.website_id
        for row in associations
        if website_domains.get(row.website_id) not in company_by_domain
    ]
    if unmapped:
        raise RuntimeError(
            "Cannot downgrade website discovery keywords: "
            f"{len(unmapped)} associations have no matching company."
        )

    op.create_table(
        "company_search_keywords",
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("search_keyword_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["search_keyword_id"],
            ["search_keywords.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("company_id", "search_keyword_id"),
    )
    for row in associations:
        company_id = company_by_domain[website_domains[row.website_id]]
        bind.execute(
            sa.insert(company_keywords).values(
                company_id=company_id,
                search_keyword_id=row.search_keyword_id,
            )
        )

    op.drop_table("website_search_keywords")
    op.create_table(
        "lead_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("source_url", sa.String(2000), nullable=False),
        sa.Column("source_domain", sa.String(255), nullable=False),
        sa.Column("source_type", sa.String(100), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_url", name="uq_lead_source_url"),
    )
