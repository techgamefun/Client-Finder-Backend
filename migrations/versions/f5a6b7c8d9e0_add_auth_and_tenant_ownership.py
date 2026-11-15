"""add organization ownership and user authentication

Revision ID: f5a6b7c8d9e0
Revises: e2f3a4b5c6d7
Create Date: 2026-11-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000001"
TENANT_TABLES = (
    "companies",
    "contacts",
    "search_keywords",
    "discovery_provider_jobs",
    "website_crawls",
    "website_corpus_analyses",
    "websites",
    "website_hypestat_profiles",
    "website_crawl_jobs",
    "application_settings",
    "pagespeed_scans",
    "pagespeed_scan_jobs",
)


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column(
            "auth_version",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
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
            "role IN ('admin', 'user')",
            name="ck_users_role_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_organization_id", "users", ["organization_id"])
    op.create_table(
        "user_provider_credentials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            name="uq_user_provider_credential",
        ),
    )
    op.create_index(
        "ix_user_provider_credentials_user_id",
        "user_provider_credentials",
        ["user_id"],
    )
    op.execute(
        sa.text(
            "INSERT INTO organizations (id, name) "
            "VALUES (:organization_id, 'Default Organization')"
        ).bindparams(
            sa.bindparam(
                "organization_id",
                value=DEFAULT_ORGANIZATION_ID,
                type_=sa.Uuid(),
            )
        )
    )

    for table in TENANT_TABLES:
        op.add_column(
            table,
            sa.Column("organization_id", sa.Uuid(), nullable=True),
        )
        op.add_column(
            table,
            sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        )
        op.execute(
            sa.text(
                f"UPDATE {table} SET organization_id = :organization_id"
            ).bindparams(
                sa.bindparam(
                    "organization_id",
                    value=DEFAULT_ORGANIZATION_ID,
                    type_=sa.Uuid(),
                )
            )
        )
        op.alter_column(table, "organization_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table}_organization_id",
            table,
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_foreign_key(
            f"fk_{table}_created_by_user_id",
            table,
            "users",
            ["created_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            f"ix_{table}_organization_id",
            table,
            ["organization_id"],
        )
        op.create_index(
            f"ix_{table}_created_by_user_id",
            table,
            ["created_by_user_id"],
        )

    op.create_unique_constraint(
        "uq_application_settings_organization",
        "application_settings",
        ["organization_id"],
    )

    op.drop_constraint("companies_domain_key", "companies", type_="unique")
    op.create_unique_constraint(
        "uq_companies_organization_domain",
        "companies",
        ["organization_id", "domain"],
    )

    op.drop_constraint(
        "website_crawls_website_url_key",
        "website_crawls",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_website_crawls_organization_url",
        "website_crawls",
        ["organization_id", "website_url"],
    )

    op.drop_constraint(
        "uq_websites_website_url",
        "websites",
        type_="unique",
    )
    op.drop_constraint("uq_websites_domain", "websites", type_="unique")
    op.create_unique_constraint(
        "uq_websites_organization_url",
        "websites",
        ["organization_id", "website_url"],
    )
    op.create_unique_constraint(
        "uq_websites_organization_domain",
        "websites",
        ["organization_id", "domain"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION notify_queue_job_event()
            RETURNS trigger AS $$
            DECLARE
                job_kind text;
            BEGIN
                IF TG_TABLE_NAME = 'website_crawl_jobs' THEN
                    job_kind := 'crawler';
                ELSE
                    job_kind := 'pagespeed';
                END IF;

                PERFORM pg_notify(
                    'queue_job_events',
                    json_build_object(
                        'kind', job_kind,
                        'id', NEW.id::text,
                        'status', NEW.status,
                        'organization_id', NEW.organization_id::text
                    )::text
                );
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )


def downgrade() -> None:
    op.drop_constraint(
        "uq_websites_organization_domain",
        "websites",
        type_="unique",
    )
    op.drop_constraint(
        "uq_websites_organization_url",
        "websites",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_websites_domain",
        "websites",
        ["domain"],
    )
    op.create_unique_constraint(
        "uq_websites_website_url",
        "websites",
        ["website_url"],
    )

    op.drop_constraint(
        "uq_website_crawls_organization_url",
        "website_crawls",
        type_="unique",
    )
    op.create_unique_constraint(
        "website_crawls_website_url_key",
        "website_crawls",
        ["website_url"],
    )
    op.drop_constraint(
        "uq_companies_organization_domain",
        "companies",
        type_="unique",
    )
    op.create_unique_constraint(
        "companies_domain_key",
        "companies",
        ["domain"],
    )
    op.drop_constraint(
        "uq_application_settings_organization",
        "application_settings",
        type_="unique",
    )

    for table in reversed(TENANT_TABLES):
        op.drop_index(f"ix_{table}_created_by_user_id", table_name=table)
        op.drop_index(f"ix_{table}_organization_id", table_name=table)
        op.drop_constraint(
            f"fk_{table}_created_by_user_id",
            table,
            type_="foreignkey",
        )
        op.drop_constraint(
            f"fk_{table}_organization_id",
            table,
            type_="foreignkey",
        )
        op.drop_column(table, "created_by_user_id")
        op.drop_column(table, "organization_id")

    op.drop_index(
        "ix_user_provider_credentials_user_id",
        table_name="user_provider_credentials",
    )
    op.drop_table("user_provider_credentials")
    op.drop_index("ix_users_organization_id", table_name="users")
    op.drop_table("users")
    op.drop_table("organizations")
