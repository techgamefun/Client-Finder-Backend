"""make the website registry domain-first and queue-oriented

Revision ID: f4a91c6d2e80
Revises: c274e8f91b62
Create Date: 2026-11-14 00:00:00.000000
"""

from typing import Any, Sequence, Union
from urllib.parse import urlsplit

from alembic import op
import sqlalchemy as sa


revision: str = "f4a91c6d2e80"
down_revision: Union[str, Sequence[str], None] = "c274e8f91b62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def upgrade() -> None:
    websites = sa.table(
        "websites",
        sa.column("id", sa.Uuid()),
        sa.column("website_url", sa.String(2000)),
        sa.column("domain", sa.String(255)),
        sa.column("crawl_id", sa.Uuid()),
        sa.column("category", sa.String(64)),
        sa.column("page_type", sa.String(64)),
        sa.column("classification_manual", sa.Boolean()),
        sa.column("topic_categories", sa.JSON()),
        sa.column("language", sa.String(35)),
        sa.column("site_type", sa.String(16)),
        sa.column("discovery_source", sa.String(255)),
        sa.column("discovery_provider", sa.String(32)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    rows = list(
        op.get_bind().execute(
            sa.select(
                websites.c.id,
                websites.c.website_url,
                websites.c.crawl_id,
                websites.c.category,
                websites.c.page_type,
                websites.c.classification_manual,
                websites.c.topic_categories,
                websites.c.language,
                websites.c.site_type,
                websites.c.discovery_source,
                websites.c.discovery_provider,
                websites.c.created_at,
            )
        )
    )
    groups: dict[str, list[Any]] = {}
    for row in rows:
        domain = _normalize_domain(row.website_url)
        if domain is None:
            raise RuntimeError(
                f"Cannot migrate website URL without a hostname: {row.website_url!r}"
            )
        groups.setdefault(domain, []).append(row)

    with op.batch_alter_table(
        "websites",
        naming_convention=_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint("uq_websites_website_url", type_="unique")

    bind = op.get_bind()
    company_fk = next(
        (
            foreign_key
            for foreign_key in sa.inspect(bind).get_foreign_keys("websites")
            if foreign_key["constrained_columns"] == ["company_id"]
            and foreign_key["referred_table"] == "companies"
        ),
        None,
    )
    company_fk_name = (
        company_fk["name"]
        if company_fk and company_fk["name"]
        else "fk_websites_company_id_companies"
        if company_fk
        else None
    )
    for domain, domain_rows in groups.items():
        ordered = sorted(
            domain_rows,
            key=lambda row: (
                row.crawl_id is None,
                str(row.created_at or ""),
                str(row.id),
            ),
        )
        keeper = ordered[0]
        site_topics = _merge_topics(domain_rows)
        page_type_row = next(
            (
                row
                for row in domain_rows
                if row.page_type and row.classification_manual
            ),
            next((row for row in domain_rows if row.page_type), None),
        )
        category = next(
            (row.category for row in ordered if row.category),
            None,
        )
        crawl_id = next(
            (row.crawl_id for row in ordered if row.crawl_id is not None),
            None,
        )
        language = next(
            (row.language for row in ordered if row.language),
            None,
        )
        provider = next(
            (row.discovery_provider for row in ordered if row.discovery_provider),
            None,
        )
        page_type = page_type_row.page_type if page_type_row else None
        classification_manual = bool(
            page_type_row and page_type_row.classification_manual
        )
        site_type = (
            "listing"
            if any(row.site_type == "listing" for row in domain_rows)
            or page_type == "business_listing"
            else "normal"
        )
        discovery_source = (
            "listing_website"
            if site_type == "listing"
            else next(
                (
                    row.discovery_source
                    for row in ordered
                    if row.discovery_source not in {
                        "manual",
                        "existing_data",
                        "existing_crawl",
                    }
                ),
                keeper.discovery_source,
            )
        )
        host = f"[{domain}]" if ":" in domain else domain
        canonical_url = f"https://{host}/"
        bind.execute(
            sa.update(websites)
            .where(websites.c.id == keeper.id)
            .values(
                website_url=canonical_url,
                domain=domain,
                crawl_id=crawl_id,
                category=category,
                page_type=page_type,
                classification_manual=classification_manual,
                topic_categories=site_topics,
                language=language,
                site_type=site_type,
                discovery_source=discovery_source,
                discovery_provider=provider,
            )
        )
        duplicate_ids = [row.id for row in domain_rows if row.id != keeper.id]
        if duplicate_ids:
            bind.execute(
                sa.delete(websites).where(websites.c.id.in_(duplicate_ids))
            )

    op.drop_index("ix_websites_domain", table_name="websites")
    op.drop_index("ix_websites_company_id", table_name="websites")
    op.drop_index("ix_websites_review_status", table_name="websites")
    with op.batch_alter_table(
        "websites",
        naming_convention=_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_listing_websites_unlinked",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_websites_review_status",
            type_="check",
        )
        if company_fk_name:
            batch_op.drop_constraint(company_fk_name, type_="foreignkey")
        batch_op.alter_column("domain", nullable=False)
        batch_op.alter_column(
            "topic_categories",
            new_column_name="site_topic",
            existing_type=sa.JSON(),
            nullable=False,
        )
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(length=20),
                server_default="new",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_websites_status_new",
            "status = 'new'",
        )
        batch_op.drop_column("company_id")
        batch_op.drop_column("review_status")
        batch_op.drop_column("snippet")
        batch_op.drop_column("title")
        batch_op.create_unique_constraint("uq_websites_domain", ["domain"])
        batch_op.create_unique_constraint(
            "uq_websites_website_url",
            ["website_url"],
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "websites",
        naming_convention=_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint("uq_websites_domain", type_="unique")
        batch_op.drop_constraint("ck_websites_status_new", type_="check")
        batch_op.alter_column("domain", nullable=True)
        batch_op.alter_column(
            "site_topic",
            new_column_name="topic_categories",
            existing_type=sa.JSON(),
            nullable=False,
        )
        batch_op.add_column(sa.Column("title", sa.String(length=500)))
        batch_op.add_column(sa.Column("snippet", sa.Text()))
        batch_op.add_column(
            sa.Column(
                "review_status",
                sa.String(length=20),
                server_default="pending",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("company_id", sa.Uuid(), nullable=True))
        batch_op.drop_column("status")
        batch_op.create_check_constraint(
            "ck_websites_review_status",
            "review_status IN ('pending', 'relevant', 'not_relevant', 'promoted')",
        )
        batch_op.create_check_constraint(
            "ck_listing_websites_unlinked",
            "site_type != 'listing' OR company_id IS NULL",
        )
        batch_op.create_foreign_key(
            "fk_websites_company_id_companies",
            "companies",
            ["company_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_websites_company_id", ["company_id"])
        batch_op.create_index("ix_websites_review_status", ["review_status"])
        batch_op.create_index("ix_websites_domain", ["domain"])


def _normalize_domain(value: str) -> str | None:
    parsed = urlsplit(value if "://" in value else f"//{value}")
    domain = parsed.hostname
    if domain is None:
        return None
    domain = domain.lower().rstrip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None


def _merge_topics(rows: list[Any]) -> list[str]:
    topics: list[str] = []
    seen: set[str] = set()
    for row in rows:
        values = row.topic_categories or []
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            topic = value.strip()
            normalized_topic = topic.casefold()
            if normalized_topic not in seen:
                topics.append(topic)
                seen.add(normalized_topic)
    return topics
