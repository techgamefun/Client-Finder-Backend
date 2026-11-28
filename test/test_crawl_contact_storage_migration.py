import importlib.util
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "a9d8c7e6f5b4_store_crawl_contacts_only_on_contacts.py"
    )
    spec = importlib.util.spec_from_file_location(
        "crawl_contact_storage_migration",
        migration_path,
    )
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_migration_moves_crawl_contact_data_to_contacts_and_drops_columns():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    crawls = sa.Table(
        "website_crawls",
        metadata,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("website_url", sa.String(2000), nullable=False),
        sa.Column("emails", sa.JSON(), nullable=False),
        sa.Column("phones", sa.JSON(), nullable=False),
    )
    websites = sa.Table(
        "websites",
        metadata,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("website_url", sa.String(2000), nullable=False),
        sa.Column("crawl_id", sa.Uuid(as_uuid=True)),
    )
    contacts = sa.Table(
        "contacts",
        metadata,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("website_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200)),
        sa.Column("role", sa.String(150)),
        sa.Column("business_email", sa.String(320)),
        sa.Column("business_phone", sa.String(50)),
        sa.Column("is_publicly_listed", sa.Boolean(), nullable=False),
    )
    metadata.create_all(engine)

    organization_id = uuid4()
    crawl_id = uuid4()
    website_id = uuid4()
    existing_contact_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            crawls.insert().values(
                id=crawl_id,
                organization_id=organization_id,
                website_url="https://example.test/",
                emails=[
                    "hello@example.test",
                    "HELLO@example.test",
                    "new@example.test",
                ],
                phones=["+1 (555) 0100", "15550100", "+1 (555) 0101"],
            )
        )
        connection.execute(
            websites.insert().values(
                id=website_id,
                organization_id=organization_id,
                website_url="https://example.test/",
                crawl_id=crawl_id,
            )
        )
        connection.execute(
            contacts.insert().values(
                id=existing_contact_id,
                website_id=website_id,
                organization_id=organization_id,
                name=None,
                role=None,
                business_email="hello@example.test",
                business_phone=None,
                is_publicly_listed=True,
            )
        )
        migration = _load_migration()
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

        saved_contacts = connection.execute(
            sa.select(contacts.c.business_email, contacts.c.business_phone)
            .where(contacts.c.website_id == website_id)
        ).all()
        crawl_columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("website_crawls")
        }

    assert set(saved_contacts) == {
        ("hello@example.test", None),
        ("new@example.test", None),
        (None, "+1 (555) 0100"),
        (None, "+1 (555) 0101"),
    }
    assert "emails" not in crawl_columns
    assert "phones" not in crawl_columns
