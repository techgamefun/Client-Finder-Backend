"""Add configurable campaign steps and Gmail delivery monitoring."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c62e8f4a1b37"
down_revision: Union[str, Sequence[str], None] = "c51a7d2e9f40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_campaigns",
        sa.Column(
            "follow_up_steps",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.add_column(
        "email_campaigns",
        sa.Column(
            "verify_mx",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column(
        "connected_gmail_accounts",
        sa.Column("last_monitor_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "connected_gmail_accounts",
        sa.Column("last_monitor_error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_connected_gmail_accounts_last_monitor_at",
        "connected_gmail_accounts",
        ["last_monitor_at"],
    )
    op.add_column(
        "email_campaign_recipients",
        sa.Column(
            "current_step",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "email_campaign_recipients",
        sa.Column(
            "sent_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "email_campaign_recipients",
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "email_campaign_recipients",
        sa.Column("bounced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "email_campaign_recipients",
        sa.Column("reply_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_email_campaign_recipients_reply_monitor",
        "email_campaign_recipients",
        ["gmail_account_id", "status", "reply_checked_at"],
    )
    op.add_column(
        "email_campaign_recipients",
        sa.Column("email_verification_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "email_campaign_recipients",
        sa.Column(
            "email_verification_checked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    with op.batch_alter_table("email_campaign_recipients") as batch:
        batch.drop_constraint(
            "ck_email_campaign_recipients_status_allowed",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_email_campaign_recipients_status_allowed",
            "status IN ('pending', 'processing', 'sent', 'failed', 'cancelled', "
            "'replied', 'bounced')",
        )
        batch.create_check_constraint(
            "ck_email_campaign_recipients_sequence_counters_nonnegative",
            "current_step >= 0 AND sent_count >= 0",
        )
    op.create_table(
        "email_campaign_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("gmail_account_id", sa.Uuid(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("to_email", sa.String(length=320), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=255), nullable=False),
        sa.Column("gmail_thread_id", sa.String(length=255), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "step_index >= 0",
            name="ck_email_campaign_deliveries_step_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["email_campaigns.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["email_campaign_recipients.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["gmail_account_id"],
            ["connected_gmail_accounts.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "recipient_id",
            "step_index",
            name="uq_email_campaign_delivery_recipient_step",
        ),
    )
    op.create_index(
        "ix_email_campaign_deliveries_org_id",
        "email_campaign_deliveries",
        ["org_id"],
    )
    op.create_index(
        "ix_email_campaign_deliveries_campaign_id",
        "email_campaign_deliveries",
        ["campaign_id"],
    )
    op.create_index(
        "ix_email_campaign_deliveries_recipient_id",
        "email_campaign_deliveries",
        ["recipient_id"],
    )
    op.create_index(
        "ix_email_campaign_deliveries_gmail_account_id",
        "email_campaign_deliveries",
        ["gmail_account_id"],
    )
    op.create_index(
        "ix_email_campaign_deliveries_sent_at",
        "email_campaign_deliveries",
        ["sent_at"],
    )
    op.create_index(
        "ix_email_campaign_deliveries_account_sent",
        "email_campaign_deliveries",
        ["gmail_account_id", "sent_at"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO email_campaign_deliveries (
                id,
                org_id,
                campaign_id,
                recipient_id,
                gmail_account_id,
                step_index,
                to_email,
                gmail_message_id,
                gmail_thread_id,
                sent_at
            )
            SELECT
                recipient.id,
                recipient.org_id,
                recipient.campaign_id,
                recipient.id,
                recipient.gmail_account_id,
                0,
                recipient.to_email,
                recipient.gmail_message_id,
                recipient.gmail_thread_id,
                recipient.sent_at
            FROM email_campaign_recipients AS recipient
            WHERE recipient.status = 'sent'
              AND recipient.gmail_message_id IS NOT NULL
              AND recipient.gmail_thread_id IS NOT NULL
              AND recipient.sent_at IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE email_campaign_recipients
            SET current_step = 1, sent_count = 1
            WHERE status = 'sent'
              AND gmail_message_id IS NOT NULL
              AND sent_at IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_campaign_deliveries_account_sent",
        table_name="email_campaign_deliveries",
    )
    op.drop_index(
        "ix_email_campaign_deliveries_sent_at",
        table_name="email_campaign_deliveries",
    )
    op.drop_index(
        "ix_email_campaign_deliveries_gmail_account_id",
        table_name="email_campaign_deliveries",
    )
    op.drop_index(
        "ix_email_campaign_deliveries_recipient_id",
        table_name="email_campaign_deliveries",
    )
    op.drop_index(
        "ix_email_campaign_deliveries_campaign_id",
        table_name="email_campaign_deliveries",
    )
    op.drop_index(
        "ix_email_campaign_deliveries_org_id",
        table_name="email_campaign_deliveries",
    )
    op.drop_table("email_campaign_deliveries")
    op.execute(
        sa.text(
            """
            UPDATE email_campaign_recipients
            SET status = 'cancelled'
            WHERE status = 'pending' AND current_step > 0
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE email_campaign_recipients
            SET status = 'sent'
            WHERE status = 'replied'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE email_campaign_recipients
            SET status = 'failed'
            WHERE status = 'bounced'
            """
        )
    )
    with op.batch_alter_table("email_campaign_recipients") as batch:
        batch.drop_constraint(
            "ck_email_campaign_recipients_sequence_counters_nonnegative",
            type_="check",
        )
        batch.drop_constraint(
            "ck_email_campaign_recipients_status_allowed",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_email_campaign_recipients_status_allowed",
            "status IN ('pending', 'processing', 'sent', 'failed', 'cancelled')",
        )
    op.drop_column(
        "email_campaign_recipients",
        "email_verification_checked_at",
    )
    op.drop_column("email_campaign_recipients", "email_verification_status")
    op.drop_index(
        "ix_email_campaign_recipients_reply_monitor",
        table_name="email_campaign_recipients",
    )
    op.drop_column("email_campaign_recipients", "reply_checked_at")
    op.drop_column("email_campaign_recipients", "bounced_at")
    op.drop_column("email_campaign_recipients", "replied_at")
    op.drop_column("email_campaign_recipients", "sent_count")
    op.drop_column("email_campaign_recipients", "current_step")
    op.drop_index(
        "ix_connected_gmail_accounts_last_monitor_at",
        table_name="connected_gmail_accounts",
    )
    op.drop_column("connected_gmail_accounts", "last_monitor_error")
    op.drop_column("connected_gmail_accounts", "last_monitor_at")
    op.drop_column("email_campaigns", "verify_mx")
    op.drop_column("email_campaigns", "follow_up_steps")
