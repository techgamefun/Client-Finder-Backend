"""add discovery review metadata

Revision ID: c19a7b3d5e21
Revises: e91a4f6b2c30
Create Date: 2026-11-13 01:30:00.000000
"""

from typing import Sequence, Union
from urllib.parse import urlsplit

from alembic import op
import sqlalchemy as sa


revision: str = "c19a7b3d5e21"
down_revision: Union[str, Sequence[str], None] = "e91a4f6b2c30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("websites", sa.Column("title", sa.String(length=500)))
    op.add_column("websites", sa.Column("domain", sa.String(length=255)))
    op.add_column("websites", sa.Column("snippet", sa.Text()))
    op.add_column("websites", sa.Column("page_type", sa.String(length=64)))
    op.add_column(
        "websites",
        sa.Column(
            "classification_manual",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "websites",
        sa.Column(
            "topic_categories",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.add_column("websites", sa.Column("language", sa.String(length=35)))
    op.add_column(
        "websites",
        sa.Column(
            "review_status",
            sa.String(length=20),
            server_default="pending",
            nullable=False,
        ),
    )
    op.create_index("ix_websites_domain", "websites", ["domain"])
    op.create_index("ix_websites_page_type", "websites", ["page_type"])
    op.create_index("ix_websites_review_status", "websites", ["review_status"])
    with op.batch_alter_table("websites") as batch_op:
        batch_op.drop_constraint("uq_websites_crawl_id", type_="unique")
        batch_op.create_check_constraint(
            "ck_websites_review_status",
            "review_status IN ('pending', 'relevant', 'not_relevant', 'promoted')",
        )

    bind = op.get_bind()
    websites = sa.table(
        "websites",
        sa.column("website_url", sa.String(2000)),
        sa.column("domain", sa.String(255)),
        sa.column("category", sa.String(64)),
        sa.column("page_type", sa.String(64)),
        sa.column("company_id", sa.Uuid()),
        sa.column("review_status", sa.String(20)),
    )
    for row in bind.execute(
        sa.select(
            websites.c.website_url,
            websites.c.category,
            websites.c.company_id,
        )
    ):
        hostname = urlsplit(
            row.website_url
            if "://" in row.website_url
            else f"//{row.website_url}"
        ).hostname
        domain = hostname.lower().rstrip(".") if hostname else None
        if domain and domain.startswith("www."):
            domain = domain[4:]
        page_type = _page_type_for_category(row.category)
        bind.execute(
            sa.update(websites)
            .where(websites.c.website_url == row.website_url)
            .values(
                domain=domain,
                page_type=page_type,
                review_status="promoted" if row.company_id else "pending",
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    websites = sa.table(
        "websites",
        sa.column("id", sa.Uuid()),
        sa.column("crawl_id", sa.Uuid()),
    )
    seen_crawl_ids = set()
    for row in bind.execute(
        sa.select(websites.c.id, websites.c.crawl_id).where(
            websites.c.crawl_id.is_not(None)
        )
    ):
        if row.crawl_id in seen_crawl_ids:
            bind.execute(
                sa.update(websites)
                .where(websites.c.id == row.id)
                .values(crawl_id=None)
            )
        else:
            seen_crawl_ids.add(row.crawl_id)

    with op.batch_alter_table("websites") as batch_op:
        batch_op.create_unique_constraint("uq_websites_crawl_id", ["crawl_id"])
        batch_op.drop_constraint("ck_websites_review_status", type_="check")
    op.drop_index("ix_websites_review_status", table_name="websites")
    op.drop_index("ix_websites_page_type", table_name="websites")
    op.drop_index("ix_websites_domain", table_name="websites")
    op.drop_column("websites", "review_status")
    op.drop_column("websites", "language")
    op.drop_column("websites", "topic_categories")
    op.drop_column("websites", "page_type")
    op.drop_column("websites", "classification_manual")
    op.drop_column("websites", "snippet")
    op.drop_column("websites", "domain")
    op.drop_column("websites", "title")


def _page_type_for_category(category: str | None) -> str | None:
    category_to_page_type = {
        "directories_listings": "business_listing",
        "news_editorial": "news",
        "ecommerce_retail": "ecommerce",
        "documentation_knowledge_base": "documentation",
        "forums_communities": "forum",
        "media_video_podcast_hubs": "media",
    }
    return category_to_page_type.get(category or "")
