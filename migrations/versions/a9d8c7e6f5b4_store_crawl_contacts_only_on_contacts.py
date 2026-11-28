"""move crawl emails and phones into contacts

Revision ID: a9d8c7e6f5b4
Revises: f6b8c2d3e4a5
Create Date: 2026-11-28 00:00:00.000000
"""

from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "a9d8c7e6f5b4"
down_revision: Union[str, Sequence[str], None] = "f6b8c2d3e4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _phone_key(value: str) -> str:
    return "".join(
        character for character in value if character in "0123456789"
    )


def upgrade() -> None:
    bind = op.get_bind()
    crawls = sa.table(
        "website_crawls",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("organization_id", sa.Uuid(as_uuid=True)),
        sa.column("website_url", sa.String(2000)),
        sa.column("emails", sa.JSON()),
        sa.column("phones", sa.JSON()),
    )
    websites = sa.table(
        "websites",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("organization_id", sa.Uuid(as_uuid=True)),
        sa.column("website_url", sa.String(2000)),
        sa.column("crawl_id", sa.Uuid(as_uuid=True)),
    )
    contacts = sa.table(
        "contacts",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("website_id", sa.Uuid(as_uuid=True)),
        sa.column("organization_id", sa.Uuid(as_uuid=True)),
        sa.column("name", sa.String(200)),
        sa.column("role", sa.String(150)),
        sa.column("business_email", sa.String(320)),
        sa.column("business_phone", sa.String(50)),
        sa.column("is_publicly_listed", sa.Boolean()),
    )

    for crawl in bind.execute(
        sa.select(
            crawls.c.id,
            crawls.c.organization_id,
            crawls.c.website_url,
            crawls.c.emails,
            crawls.c.phones,
        )
    ):
        emails = crawl.emails if isinstance(crawl.emails, list) else []
        phones = crawl.phones if isinstance(crawl.phones, list) else []
        if not emails and not phones:
            continue

        website_ids = list(
            bind.scalars(
                sa.select(websites.c.id).where(
                    websites.c.organization_id == crawl.organization_id,
                    sa.or_(
                        websites.c.crawl_id == crawl.id,
                        websites.c.website_url == crawl.website_url,
                    ),
                )
            )
        )
        if not website_ids:
            raise RuntimeError(
                f"Website crawl {crawl.id} contains contact data but has no "
                "linked website in the same organization."
            )

        for website_id in website_ids:
            existing_contacts = bind.execute(
                sa.select(
                    contacts.c.business_email,
                    contacts.c.business_phone,
                ).where(contacts.c.website_id == website_id)
            )
            existing_emails: set[str] = set()
            existing_phones: set[str] = set()
            for existing in existing_contacts:
                if existing.business_email:
                    existing_emails.add(
                        existing.business_email.strip().casefold()
                    )
                if existing.business_phone:
                    phone_key = _phone_key(existing.business_phone)
                    if phone_key:
                        existing_phones.add(phone_key)

            for email in emails:
                if not isinstance(email, str):
                    continue
                normalized_email = email.strip().casefold()
                if not normalized_email or normalized_email in existing_emails:
                    continue
                bind.execute(
                    sa.insert(contacts).values(
                        id=uuid4(),
                        website_id=website_id,
                        organization_id=crawl.organization_id,
                        name=None,
                        role=None,
                        business_email=normalized_email,
                        business_phone=None,
                        is_publicly_listed=True,
                    )
                )
                existing_emails.add(normalized_email)

            for phone in phones:
                if not isinstance(phone, str):
                    continue
                normalized_phone = phone.strip()
                phone_key = _phone_key(normalized_phone)
                if (
                    not normalized_phone
                    or not phone_key
                    or phone_key in existing_phones
                ):
                    continue
                bind.execute(
                    sa.insert(contacts).values(
                        id=uuid4(),
                        website_id=website_id,
                        organization_id=crawl.organization_id,
                        name=None,
                        role=None,
                        business_email=None,
                        business_phone=normalized_phone,
                        is_publicly_listed=True,
                    )
                )
                existing_phones.add(phone_key)

    with op.batch_alter_table("website_crawls") as batch:
        batch.drop_column("emails")
        batch.drop_column("phones")


def downgrade() -> None:
    raise RuntimeError(
        "This migration is intentionally irreversible because historical "
        "crawl-level contact snapshots are consolidated into Contact records."
    )
