"""convert entity and foreign keys to UUIDs

Revision ID: e83b9fd41c26
Revises: c7a61f8290be
Create Date: 2026-11-10 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e83b9fd41c26"
down_revision: Union[str, Sequence[str], None] = "c7a61f8290be"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ID_TABLES = (
    "companies",
    "contacts",
    "lead_sources",
    "search_keywords",
    "website_crawls",
    "pagespeed_scans",
)


def _create_uuid_maps() -> None:
    for table in ID_TABLES:
        op.execute(
            sa.text(
                f"CREATE TEMPORARY TABLE _uuid_{table}_map AS "
                f"SELECT id AS old_id, gen_random_uuid() AS new_id FROM {table}"
            )
        )


def upgrade() -> None:
    _create_uuid_maps()

    for table in ID_TABLES:
        op.add_column(table, sa.Column("new_id", sa.Uuid(), nullable=True))
        op.execute(
            sa.text(
                f"UPDATE {table} AS target SET new_id = mapping.new_id "
                f"FROM _uuid_{table}_map AS mapping "
                "WHERE target.id = mapping.old_id"
            )
        )

    for table in ("contacts", "lead_sources", "pagespeed_scans"):
        op.add_column(
            table,
            sa.Column("new_company_id", sa.Uuid(), nullable=True),
        )
        op.execute(
            sa.text(
                f"UPDATE {table} AS target "
                "SET new_company_id = mapping.new_id "
                "FROM _uuid_companies_map AS mapping "
                "WHERE target.company_id = mapping.old_id"
            )
        )

    op.add_column(
        "company_search_keywords",
        sa.Column("new_company_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "company_search_keywords",
        sa.Column("new_search_keyword_id", sa.Uuid(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE company_search_keywords AS target "
            "SET new_company_id = companies.new_id "
            "FROM _uuid_companies_map AS companies "
            "WHERE target.company_id = companies.old_id"
        )
    )
    op.execute(
        sa.text(
            "UPDATE company_search_keywords AS target "
            "SET new_search_keyword_id = keywords.new_id "
            "FROM _uuid_search_keywords_map AS keywords "
            "WHERE target.search_keyword_id = keywords.old_id"
        )
    )

    for constraint, table in (
        ("contacts_company_id_fkey", "contacts"),
        ("lead_sources_company_id_fkey", "lead_sources"),
        ("pagespeed_scans_company_id_fkey", "pagespeed_scans"),
        ("company_search_keywords_company_id_fkey", "company_search_keywords"),
        (
            "company_search_keywords_search_keyword_id_fkey",
            "company_search_keywords",
        ),
    ):
        op.drop_constraint(constraint, table, type_="foreignkey")

    op.execute(
        sa.text(
            "DROP INDEX IF EXISTS ix_pagespeed_scans_company_scanned_at"
        )
    )

    for table in (*ID_TABLES, "company_search_keywords"):
        op.drop_constraint(f"{table}_pkey", table, type_="primary")

    for table in ID_TABLES:
        op.drop_column(table, "id")
        op.alter_column(table, "new_id", new_column_name="id", nullable=False)

    for table in ("contacts", "lead_sources", "pagespeed_scans"):
        op.drop_column(table, "company_id")
        op.alter_column(
            table,
            "new_company_id",
            new_column_name="company_id",
            nullable=False,
        )

    op.drop_column("company_search_keywords", "company_id")
    op.drop_column("company_search_keywords", "search_keyword_id")
    op.alter_column(
        "company_search_keywords",
        "new_company_id",
        new_column_name="company_id",
        nullable=False,
    )
    op.alter_column(
        "company_search_keywords",
        "new_search_keyword_id",
        new_column_name="search_keyword_id",
        nullable=False,
    )

    for table in ID_TABLES:
        op.create_primary_key(f"{table}_pkey", table, ["id"])
    op.create_primary_key(
        "company_search_keywords_pkey",
        "company_search_keywords",
        ["company_id", "search_keyword_id"],
    )

    for table, constraint in (
        ("contacts", "contacts_company_id_fkey"),
        ("lead_sources", "lead_sources_company_id_fkey"),
        ("pagespeed_scans", "pagespeed_scans_company_id_fkey"),
    ):
        op.create_foreign_key(
            constraint,
            table,
            "companies",
            ["company_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_foreign_key(
        "company_search_keywords_company_id_fkey",
        "company_search_keywords",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "company_search_keywords_search_keyword_id_fkey",
        "company_search_keywords",
        "search_keywords",
        ["search_keyword_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_pagespeed_scans_company_scanned_at",
        "pagespeed_scans",
        ["company_id", "scanned_at"],
        unique=False,
    )


def downgrade() -> None:
    for table in ID_TABLES:
        op.execute(
            sa.text(
                f"CREATE TEMPORARY TABLE _int_{table}_map AS "
                "SELECT id AS old_id, "
                "row_number() OVER (ORDER BY id)::integer AS new_id "
                f"FROM {table}"
            )
        )

    for table in ID_TABLES:
        op.add_column(table, sa.Column("new_id", sa.Integer(), nullable=True))
        op.execute(
            sa.text(
                f"UPDATE {table} AS target SET new_id = mapping.new_id "
                f"FROM _int_{table}_map AS mapping "
                "WHERE target.id = mapping.old_id"
            )
        )

    for table in ("contacts", "lead_sources", "pagespeed_scans"):
        op.add_column(table, sa.Column("new_company_id", sa.Integer()))
        op.execute(
            sa.text(
                f"UPDATE {table} AS target "
                "SET new_company_id = mapping.new_id "
                "FROM _int_companies_map AS mapping "
                "WHERE target.company_id = mapping.old_id"
            )
        )

    op.add_column(
        "company_search_keywords",
        sa.Column("new_company_id", sa.Integer()),
    )
    op.add_column(
        "company_search_keywords",
        sa.Column("new_search_keyword_id", sa.Integer()),
    )
    op.execute(
        sa.text(
            "UPDATE company_search_keywords AS target "
            "SET new_company_id = companies.new_id "
            "FROM _int_companies_map AS companies "
            "WHERE target.company_id = companies.old_id"
        )
    )
    op.execute(
        sa.text(
            "UPDATE company_search_keywords AS target "
            "SET new_search_keyword_id = keywords.new_id "
            "FROM _int_search_keywords_map AS keywords "
            "WHERE target.search_keyword_id = keywords.old_id"
        )
    )

    for constraint, table in (
        ("contacts_company_id_fkey", "contacts"),
        ("lead_sources_company_id_fkey", "lead_sources"),
        ("pagespeed_scans_company_id_fkey", "pagespeed_scans"),
        ("company_search_keywords_company_id_fkey", "company_search_keywords"),
        (
            "company_search_keywords_search_keyword_id_fkey",
            "company_search_keywords",
        ),
    ):
        op.drop_constraint(constraint, table, type_="foreignkey")

    op.execute(
        sa.text(
            "DROP INDEX IF EXISTS ix_pagespeed_scans_company_scanned_at"
        )
    )

    for table in (*ID_TABLES, "company_search_keywords"):
        op.drop_constraint(f"{table}_pkey", table, type_="primary")

    for table in ID_TABLES:
        op.drop_column(table, "id")
        op.alter_column(table, "new_id", new_column_name="id", nullable=False)

    for table in ("contacts", "lead_sources", "pagespeed_scans"):
        op.drop_column(table, "company_id")
        op.alter_column(
            table,
            "new_company_id",
            new_column_name="company_id",
            nullable=False,
        )

    op.drop_column("company_search_keywords", "company_id")
    op.drop_column("company_search_keywords", "search_keyword_id")
    op.alter_column(
        "company_search_keywords",
        "new_company_id",
        new_column_name="company_id",
        nullable=False,
    )
    op.alter_column(
        "company_search_keywords",
        "new_search_keyword_id",
        new_column_name="search_keyword_id",
        nullable=False,
    )

    for table in ID_TABLES:
        op.create_primary_key(f"{table}_pkey", table, ["id"])
        op.execute(sa.text(f"CREATE SEQUENCE {table}_id_seq"))
        op.execute(
            sa.text(
                f"SELECT setval('{table}_id_seq', "
                f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
            )
        )
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ALTER COLUMN id "
                f"SET DEFAULT nextval('{table}_id_seq')"
            )
        )
        op.execute(
            sa.text(
                f"ALTER SEQUENCE {table}_id_seq OWNED BY {table}.id"
            )
        )
    op.create_primary_key(
        "company_search_keywords_pkey",
        "company_search_keywords",
        ["company_id", "search_keyword_id"],
    )

    for table, constraint in (
        ("contacts", "contacts_company_id_fkey"),
        ("lead_sources", "lead_sources_company_id_fkey"),
        ("pagespeed_scans", "pagespeed_scans_company_id_fkey"),
    ):
        op.create_foreign_key(
            constraint,
            table,
            "companies",
            ["company_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_foreign_key(
        "company_search_keywords_company_id_fkey",
        "company_search_keywords",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "company_search_keywords_search_keyword_id_fkey",
        "company_search_keywords",
        "search_keywords",
        ["search_keyword_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_pagespeed_scans_company_scanned_at",
        "pagespeed_scans",
        ["company_id", "scanned_at"],
        unique=False,
    )
