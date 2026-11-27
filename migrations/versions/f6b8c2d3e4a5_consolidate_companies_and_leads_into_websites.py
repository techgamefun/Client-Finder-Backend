"""consolidate company and lead data into websites

Revision ID: f6b8c2d3e4a5
Revises: e6f4a9d2c731
Create Date: 2026-11-29 00:00:00.000000
"""

from typing import Sequence, Union
from uuid import UUID, uuid4

from alembic import op
import sqlalchemy as sa

from app.services.normalization import normalize_company_name, normalize_domain


revision: str = "f6b8c2d3e4a5"
down_revision: Union[str, Sequence[str], None] = "e6f4a9d2c731"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


WEBSITE_STATUSES = {
    "new",
    "checked",
    "qualified",
    "contacted",
    "converted",
    "disqualified",
    "needs_review",
}
STATUS_PRIORITY = {
    "new": 0,
    "needs_review": 1,
    "checked": 2,
    "qualified": 3,
    "contacted": 4,
    "converted": 5,
    "disqualified": 6,
}
FK_NAMING = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def _workflow_status(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized == "lead":
        return "qualified"
    if normalized in WEBSITE_STATUSES:
        return normalized
    return "needs_review"


def _domain_url(domain: str) -> str:
    host = f"[{domain}]" if ":" in domain else domain
    return f"https://{host}/"


def _create_website_groups() -> None:
    op.create_table(
        "website_groups",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
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
    )
    op.create_index(
        "ix_website_groups_organization_id",
        "website_groups",
        ["organization_id"],
    )
    op.create_index(
        "ix_website_groups_created_by_user_id",
        "website_groups",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_website_groups_organization_created_at",
        "website_groups",
        ["organization_id", "created_at"],
    )
    op.create_table(
        "website_group_websites",
        sa.Column("website_group_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("website_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["website_group_id"],
            ["website_groups.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "website_group_id",
            "website_id",
            name="pk_website_group_websites",
        ),
    )
    op.create_index(
        "ix_website_group_websites_website_id",
        "website_group_websites",
        ["website_id"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    companies = sa.table(
        "companies",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("name", sa.String(200)),
        sa.column("website", sa.String(500)),
        sa.column("domain", sa.String(255)),
        sa.column("industry", sa.String(150)),
        sa.column("city", sa.String(100)),
        sa.column("state", sa.String(100)),
        sa.column("software_need", sa.Text()),
        sa.column("lead_score", sa.Integer()),
        sa.column("lead_status", sa.String(30)),
        sa.column("last_email_sent_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("organization_id", sa.Uuid(as_uuid=True)),
        sa.column("created_by_user_id", sa.Uuid(as_uuid=True)),
    )
    websites = sa.table(
        "websites",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("organization_id", sa.Uuid(as_uuid=True)),
        sa.column("created_by_user_id", sa.Uuid(as_uuid=True)),
        sa.column("website_url", sa.String(2000)),
        sa.column("domain", sa.String(255)),
        sa.column("business_name", sa.String(200)),
        sa.column("industry", sa.String(150)),
        sa.column("city", sa.String(100)),
        sa.column("state", sa.String(100)),
        sa.column("software_need", sa.Text()),
        sa.column("lead_score", sa.Integer()),
        sa.column("last_email_sent_at", sa.DateTime(timezone=True)),
        sa.column("domain_resolution_needed", sa.Boolean()),
        sa.column("status", sa.String(20)),
        sa.column("discovery_source", sa.String(255)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    contacts = sa.table(
        "contacts",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("company_id", sa.Uuid(as_uuid=True)),
        sa.column("website_id", sa.Uuid(as_uuid=True)),
    )

    for column in (
        sa.Column("business_name", sa.String(length=200), nullable=True),
        sa.Column("industry", sa.String(length=150), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("state", sa.String(length=100), nullable=True),
        sa.Column("software_need", sa.Text(), nullable=True),
        sa.Column("lead_score", sa.Integer(), nullable=True),
        sa.Column("last_email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "domain_resolution_needed",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    ):
        op.add_column("websites", column)

    op.add_column(
        "contacts",
        sa.Column("website_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.add_column(
        "email_campaign_recipients",
        sa.Column("website_id", sa.Uuid(as_uuid=True), nullable=True),
    )

    with op.batch_alter_table("websites", naming_convention=FK_NAMING) as batch:
        batch.drop_constraint("ck_websites_status_allowed", type_="check")

    existing_websites: dict[tuple[UUID, str], UUID] = {}
    for row in bind.execute(
        sa.select(
            websites.c.id,
            websites.c.organization_id,
            websites.c.domain,
        )
    ):
        domain = normalize_domain(row.domain)
        if domain:
            existing_websites[(row.organization_id, domain)] = row.id

    company_to_website: dict[UUID, UUID] = {}
    company_rows = bind.execute(
        sa.select(
            companies.c.id,
            companies.c.name,
            companies.c.website,
            companies.c.domain,
            companies.c.industry,
            companies.c.city,
            companies.c.state,
            companies.c.software_need,
            companies.c.lead_score,
            companies.c.lead_status,
            companies.c.last_email_sent_at,
            companies.c.created_at,
            companies.c.organization_id,
            companies.c.created_by_user_id,
        ).order_by(companies.c.created_at, companies.c.id)
    )
    for company in company_rows:
        domain = normalize_domain(company.domain) or normalize_domain(
            company.website
        )
        needs_resolution = domain is None
        if domain is None:
            domain = f"company-{company.id}.invalid"
        website_id = existing_websites.get((company.organization_id, domain))
        if website_id is None:
            website_id = uuid4()
            bind.execute(
                sa.insert(websites).values(
                    id=website_id,
                    organization_id=company.organization_id,
                    created_by_user_id=company.created_by_user_id,
                    website_url=_domain_url(domain),
                    domain=domain,
                    business_name=company.name,
                    industry=company.industry,
                    city=company.city,
                    state=company.state,
                    software_need=company.software_need,
                    lead_score=company.lead_score,
                    last_email_sent_at=company.last_email_sent_at,
                    domain_resolution_needed=needs_resolution,
                    status=_workflow_status(company.lead_status),
                    discovery_source="existing_data",
                    created_at=company.created_at,
                )
            )
            existing_websites[(company.organization_id, domain)] = website_id
        else:
            current = bind.execute(
                sa.select(
                    websites.c.business_name,
                    websites.c.industry,
                    websites.c.city,
                    websites.c.state,
                    websites.c.software_need,
                    websites.c.lead_score,
                    websites.c.last_email_sent_at,
                    websites.c.created_by_user_id,
                    websites.c.status,
                ).where(websites.c.id == website_id)
            ).one()
            values = {
                "business_name": current.business_name or company.name,
                "industry": current.industry or company.industry,
                "city": current.city or company.city,
                "state": current.state or company.state,
                "software_need": current.software_need or company.software_need,
                "lead_score": (
                    current.lead_score
                    if current.lead_score is not None
                    else company.lead_score
                ),
                "last_email_sent_at": (
                    current.last_email_sent_at or company.last_email_sent_at
                ),
                "created_by_user_id": (
                    current.created_by_user_id or company.created_by_user_id
                ),
            }
            current_status = _workflow_status(current.status)
            company_status = _workflow_status(company.lead_status)
            if STATUS_PRIORITY[company_status] > STATUS_PRIORITY[current_status]:
                values["status"] = company_status
            bind.execute(
                sa.update(websites)
                .where(websites.c.id == website_id)
                .values(**values)
            )
        company_to_website[company.id] = website_id

    for company_id, website_id in company_to_website.items():
        bind.execute(
            sa.update(contacts)
            .where(contacts.c.company_id == company_id)
            .values(website_id=website_id)
        )

    recipients = sa.table(
        "email_campaign_recipients",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("contact_id", sa.Uuid(as_uuid=True)),
        sa.column("website_id", sa.Uuid(as_uuid=True)),
        sa.column("status", sa.String(20)),
        sa.column("sent_at", sa.DateTime(timezone=True)),
    )
    scans = sa.table(
        "pagespeed_scans",
        sa.column("company_id", sa.Uuid(as_uuid=True)),
        sa.column("website_id", sa.Uuid(as_uuid=True)),
    )
    jobs = sa.table(
        "pagespeed_scan_jobs",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("organization_id", sa.Uuid(as_uuid=True)),
        sa.column("website_url", sa.String(2000)),
        sa.column("company_id", sa.Uuid(as_uuid=True)),
        sa.column("website_id", sa.Uuid(as_uuid=True)),
    )

    bind.execute(
        sa.update(recipients).values(
            website_id=sa.select(contacts.c.website_id)
            .where(contacts.c.id == recipients.c.contact_id)
            .scalar_subquery()
        )
    )
    for company_id, website_id in company_to_website.items():
        bind.execute(
            sa.update(scans)
            .where(
                scans.c.company_id == company_id,
                scans.c.website_id.is_(None),
            )
            .values(website_id=website_id)
        )
        bind.execute(
            sa.update(jobs)
            .where(
                jobs.c.company_id == company_id,
                jobs.c.website_id.is_(None),
            )
            .values(website_id=website_id)
        )

    job_company_links = sa.table(
        "pagespeed_scan_job_companies",
        sa.column("job_id", sa.Uuid(as_uuid=True)),
        sa.column("company_id", sa.Uuid(as_uuid=True)),
    )
    linked_websites: dict[UUID, set[UUID]] = {}
    for row in bind.execute(sa.select(job_company_links)):
        website_id = company_to_website.get(row.company_id)
        if website_id is not None:
            linked_websites.setdefault(row.job_id, set()).add(website_id)

    website_domains = {
        website_id: domain
        for (_, domain), website_id in existing_websites.items()
    }
    for job in bind.execute(
        sa.select(
            jobs.c.id,
            jobs.c.organization_id,
            jobs.c.website_url,
            jobs.c.website_id,
        )
    ):
        if job.website_id is not None:
            continue
        candidates = linked_websites.get(job.id, set())
        if not candidates:
            continue
        if len(candidates) == 1:
            website_id = next(iter(candidates))
        else:
            job_domain = normalize_domain(job.website_url)
            matching = {
                candidate
                for candidate in candidates
                if website_domains.get(candidate) == job_domain
            }
            if len(matching) != 1:
                raise RuntimeError(
                    f"PageSpeed job {job.id} has ambiguous legacy website owners."
                )
            website_id = next(iter(matching))
        bind.execute(
            sa.update(jobs)
            .where(jobs.c.id == job.id, jobs.c.website_id.is_(None))
            .values(website_id=website_id)
        )

    _create_website_groups()
    lead_batches = sa.table(
        "lead_batches",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("organization_id", sa.Uuid(as_uuid=True)),
        sa.column("created_by_user_id", sa.Uuid(as_uuid=True)),
        sa.column("name", sa.String(160)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    groups = sa.table(
        "website_groups",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("organization_id", sa.Uuid(as_uuid=True)),
        sa.column("created_by_user_id", sa.Uuid(as_uuid=True)),
        sa.column("name", sa.String(160)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    for row in bind.execute(sa.select(lead_batches)):
        bind.execute(
            sa.insert(groups).values(
                id=row.id,
                organization_id=row.organization_id,
                created_by_user_id=row.created_by_user_id,
                name=row.name,
                created_at=row.created_at,
            )
        )

    old_members = sa.table(
        "lead_batch_companies",
        sa.column("lead_batch_id", sa.Uuid(as_uuid=True)),
        sa.column("company_id", sa.Uuid(as_uuid=True)),
    )
    new_members = sa.table(
        "website_group_websites",
        sa.column("website_group_id", sa.Uuid(as_uuid=True)),
        sa.column("website_id", sa.Uuid(as_uuid=True)),
    )
    inserted_members: set[tuple[UUID, UUID]] = set()
    for row in bind.execute(sa.select(old_members)):
        website_id = company_to_website[row.company_id]
        pair = (row.lead_batch_id, website_id)
        if pair not in inserted_members:
            bind.execute(
                sa.insert(new_members).values(
                    website_group_id=pair[0],
                    website_id=pair[1],
                )
            )
            inserted_members.add(pair)

    bind.execute(
        sa.text(
            """
            UPDATE websites
            SET last_email_sent_at = (
                SELECT MAX(recipients.sent_at)
                FROM email_campaign_recipients AS recipients
                WHERE recipients.website_id = websites.id
                  AND recipients.status = 'sent'
                  AND recipients.sent_at IS NOT NULL
            )
            WHERE EXISTS (
                SELECT 1
                FROM email_campaign_recipients AS recipients
                WHERE recipients.website_id = websites.id
                  AND recipients.status = 'sent'
                  AND recipients.sent_at IS NOT NULL
            )
            """
        )
    )
    bind.execute(
        sa.update(websites)
        .where(websites.c.status == "lead")
        .values(status="qualified")
    )
    templates = sa.table(
        "email_templates",
        sa.column("subject", sa.String(998)),
        sa.column("body_content", sa.Text()),
    )
    bind.execute(
        sa.update(templates).values(
            subject=sa.func.replace(templates.c.subject, "company.", "website."),
            body_content=sa.func.replace(
                templates.c.body_content,
                "company.",
                "website.",
            ),
        )
    )

    op.drop_index(
        "ix_email_campaign_recipients_company_id",
        table_name="email_campaign_recipients",
    )
    op.drop_index(
        "ix_contacts_company_organization",
        table_name="contacts",
    )
    op.drop_index(
        "ix_pagespeed_scans_company_scanned_at",
        table_name="pagespeed_scans",
    )
    for table_name in ("contacts", "email_campaign_recipients"):
        with op.batch_alter_table(
            table_name,
            naming_convention=FK_NAMING,
        ) as batch:
            batch.drop_column("company_id")
            batch.alter_column(
                "website_id",
                existing_type=sa.Uuid(as_uuid=True),
                nullable=False,
            )
            batch.create_foreign_key(
                f"fk_{table_name}_website_id_websites",
                "websites",
                ["website_id"],
                ["id"],
                ondelete="CASCADE",
            )
    for table_name in ("pagespeed_scans", "pagespeed_scan_jobs"):
        with op.batch_alter_table(
            table_name,
            naming_convention=FK_NAMING,
        ) as batch:
            batch.drop_constraint(
                f"ck_{table_name}_has_owner",
                type_="check",
            )
            batch.drop_column("company_id")
            batch.alter_column(
                "website_id",
                existing_type=sa.Uuid(as_uuid=True),
                nullable=False,
            )
    op.create_index(
        "ix_contacts_website_organization",
        "contacts",
        ["website_id", "organization_id"],
    )
    op.create_index(
        "ix_email_campaign_recipients_website_id",
        "email_campaign_recipients",
        ["website_id"],
    )

    op.drop_table("pagespeed_scan_job_companies")

    op.drop_index(
        "ix_lead_batch_companies_company_id",
        table_name="lead_batch_companies",
    )
    op.drop_table("lead_batch_companies")
    op.drop_index(
        "ix_lead_batches_organization_created_at",
        table_name="lead_batches",
    )
    op.drop_index("ix_lead_batches_organization_id", table_name="lead_batches")
    op.drop_index("ix_lead_batches_created_by_user_id", table_name="lead_batches")
    op.drop_table("lead_batches")
    op.drop_table("companies")

    with op.batch_alter_table("websites", naming_convention=FK_NAMING) as batch:
        batch.create_check_constraint(
            "ck_websites_status_allowed",
            "status IN ('new', 'checked', 'qualified', 'contacted', "
            "'converted', 'disqualified', 'needs_review')",
        )


def downgrade() -> None:
    raise RuntimeError(
        "This migration is intentionally irreversible because company records "
        "are consolidated into website profiles."
    )
