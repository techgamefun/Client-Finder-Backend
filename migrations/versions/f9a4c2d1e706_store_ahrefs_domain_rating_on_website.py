"""Store Ahrefs Domain Rating independently from paid metrics.

Revision ID: f9a4c2d1e706
Revises: e6f4a1b3c9d2, b4a7c9d2e610
"""

from alembic import op
import sqlalchemy as sa


revision = "f9a4c2d1e706"
down_revision = ("e6f4a1b3c9d2", "b4a7c9d2e610")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "websites",
        sa.Column("ahrefs_domain_rating", sa.Float(), nullable=True),
    )
    op.add_column(
        "websites",
        sa.Column(
            "ahrefs_domain_rating_license_url",
            sa.String(length=2048),
            nullable=True,
        ),
    )
    op.add_column(
        "websites",
        sa.Column(
            "ahrefs_domain_rating_fetched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    metrics = sa.table(
        "website_ahrefs_metrics",
        sa.column("website_id", sa.Uuid()),
        sa.column("domain_rating", sa.Float()),
        sa.column("domain_rating_license_url", sa.String(length=2048)),
        sa.column("fetched_at", sa.DateTime(timezone=True)),
        sa.column("organic_traffic", sa.Integer()),
        sa.column("organic_traffic_cost_cents", sa.Integer()),
        sa.column("organic_keywords", sa.Integer()),
        sa.column("backlinks_live", sa.Integer()),
        sa.column("backlinks_all_time", sa.Integer()),
        sa.column("refdomains_live", sa.Integer()),
        sa.column("refdomains_all_time", sa.Integer()),
        sa.column("units_used", sa.Integer()),
    )
    websites = sa.table(
        "websites",
        sa.column("id", sa.Uuid()),
        sa.column("ahrefs_domain_rating", sa.Float()),
        sa.column("ahrefs_domain_rating_license_url", sa.String(length=2048)),
        sa.column("ahrefs_domain_rating_fetched_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            metrics.c.website_id,
            metrics.c.domain_rating,
            metrics.c.domain_rating_license_url,
            metrics.c.fetched_at,
        ).order_by(metrics.c.fetched_at.desc())
    )
    seen_website_ids = set()
    for row in rows:
        if row.website_id in seen_website_ids:
            continue
        connection.execute(
            sa.update(websites)
            .where(websites.c.id == row.website_id)
            .values(
                ahrefs_domain_rating=row.domain_rating,
                ahrefs_domain_rating_license_url=(
                    row.domain_rating_license_url
                ),
                ahrefs_domain_rating_fetched_at=row.fetched_at,
            )
        )
        seen_website_ids.add(row.website_id)

    connection.execute(
        sa.delete(metrics).where(
            metrics.c.units_used == 0,
            metrics.c.organic_traffic.is_(None),
            metrics.c.organic_traffic_cost_cents.is_(None),
            metrics.c.organic_keywords.is_(None),
            metrics.c.backlinks_live.is_(None),
            metrics.c.backlinks_all_time.is_(None),
            metrics.c.refdomains_live.is_(None),
            metrics.c.refdomains_all_time.is_(None),
        )
    )


def downgrade() -> None:
    op.drop_column("websites", "ahrefs_domain_rating_fetched_at")
    op.drop_column("websites", "ahrefs_domain_rating_license_url")
    op.drop_column("websites", "ahrefs_domain_rating")
