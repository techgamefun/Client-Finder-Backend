"""store provider keys as plaintext

Revision ID: b7e4c2a9d681
Revises: a92f5d6e3c11
Create Date: 2026-11-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7e4c2a9d681"
down_revision: Union[str, Sequence[str], None] = "a92f5d6e3c11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing values are ciphertext, so they cannot be used as plaintext keys.
    op.execute(sa.text("DELETE FROM user_provider_credentials"))
    op.alter_column(
        "user_provider_credentials",
        "api_key_encrypted",
        new_column_name="api_key",
        existing_type=sa.Text(),
        existing_nullable=False,
    )
    op.drop_column("application_settings", "tavily_api_key_encrypted")
    op.drop_column("application_settings", "octoparse_api_key_encrypted")
    op.drop_column("application_settings", "hypestat_api_key_encrypted")


def downgrade() -> None:
    # Plaintext credentials must not be presented as encrypted credentials.
    op.execute(sa.text("DELETE FROM user_provider_credentials"))
    op.alter_column(
        "user_provider_credentials",
        "api_key",
        new_column_name="api_key_encrypted",
        existing_type=sa.Text(),
        existing_nullable=False,
    )
    op.add_column(
        "application_settings",
        sa.Column("tavily_api_key_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "application_settings",
        sa.Column("octoparse_api_key_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "application_settings",
        sa.Column("hypestat_api_key_encrypted", sa.Text(), nullable=True),
    )
