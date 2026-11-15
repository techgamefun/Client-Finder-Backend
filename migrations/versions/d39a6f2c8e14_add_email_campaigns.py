"""Add multi-account email drip campaigns."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d39a6f2c8e14"
down_revision = "f68b2a1c9d43"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_campaigns",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("template_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "gmail_account_ids",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column(
            "daily_limit_per_account",
            sa.Integer(),
            server_default="40",
            nullable=False,
        ),
        sa.Column(
            "min_delay_seconds",
            sa.Integer(),
            server_default="180",
            nullable=False,
        ),
        sa.Column(
            "max_delay_seconds",
            sa.Integer(),
            server_default="420",
            nullable=False,
        ),
        sa.Column(
            "send_window_start_hour",
            sa.Integer(),
            server_default="9",
            nullable=False,
        ),
        sa.Column(
            "send_window_end_hour",
            sa.Integer(),
            server_default="17",
            nullable=False,
        ),
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default="UTC",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="draft",
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
            "status IN ('draft', 'running', 'paused', 'completed')",
            name="ck_email_campaigns_status_allowed",
        ),
        sa.CheckConstraint(
            "daily_limit_per_account > 0",
            name="ck_email_campaigns_daily_limit_positive",
        ),
        sa.CheckConstraint(
            "min_delay_seconds >= 1 AND max_delay_seconds >= min_delay_seconds",
            name="ck_email_campaigns_delay_range",
        ),
        sa.CheckConstraint(
            "send_window_start_hour >= 0 AND send_window_start_hour < send_window_end_hour "
            "AND send_window_end_hour <= 23",
            name="ck_email_campaigns_send_window",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["email_templates.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_campaigns_org_id",
        "email_campaigns",
        ["org_id"],
    )
    op.create_index(
        "ix_email_campaigns_created_by",
        "email_campaigns",
        ["created_by"],
    )
    op.create_index(
        "ix_email_campaigns_template_id",
        "email_campaigns",
        ["template_id"],
    )

    op.create_table(
        "email_campaign_recipients",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("campaign_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("contact_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("gmail_account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("to_email", sa.String(length=320), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("gmail_message_id", sa.String(length=255), nullable=True),
        sa.Column("gmail_thread_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('pending', 'processing', 'sent', 'failed', 'cancelled')",
            name="ck_email_campaign_recipients_status_allowed",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_email_campaign_recipients_attempts_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["email_campaigns.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["gmail_account_id"],
            ["connected_gmail_accounts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            "contact_id",
            name="uq_email_campaign_recipient_contact",
        ),
    )
    op.create_index(
        "ix_email_campaign_recipients_org_id",
        "email_campaign_recipients",
        ["org_id"],
    )
    op.create_index(
        "ix_email_campaign_recipients_campaign_id",
        "email_campaign_recipients",
        ["campaign_id"],
    )
    op.create_index(
        "ix_email_campaign_recipients_company_id",
        "email_campaign_recipients",
        ["company_id"],
    )
    op.create_index(
        "ix_email_campaign_recipients_contact_id",
        "email_campaign_recipients",
        ["contact_id"],
    )
    op.create_index(
        "ix_email_campaign_recipients_gmail_account_id",
        "email_campaign_recipients",
        ["gmail_account_id"],
    )
    op.create_index(
        "ix_email_campaign_recipients_scheduled_at",
        "email_campaign_recipients",
        ["scheduled_at"],
    )
    op.create_index(
        "ix_email_campaign_recipients_due",
        "email_campaign_recipients",
        ["status", "scheduled_at"],
    )
    op.create_index(
        "ix_email_campaign_recipients_campaign_status",
        "email_campaign_recipients",
        ["campaign_id", "status"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION notify_email_campaign_event() RETURNS trigger AS $$
            DECLARE
                event_kind text;
                campaign_ref text;
            BEGIN
                IF TG_TABLE_NAME = 'email_campaigns' THEN
                    event_kind := 'email_campaign';
                    campaign_ref := NEW.id::text;
                ELSE
                    event_kind := 'email_campaign_recipient';
                    campaign_ref := NEW.campaign_id::text;
                END IF;

                PERFORM pg_notify(
                    'queue_job_events',
                    json_build_object(
                        'kind', event_kind,
                        'id', NEW.id::text,
                        'campaign_id', campaign_ref,
                        'status', NEW.status,
                        'organization_id', NEW.org_id::text
                    )::text
                );
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER email_campaign_status_event
            AFTER UPDATE OF status ON email_campaigns
            FOR EACH ROW
            WHEN (OLD.status IS DISTINCT FROM NEW.status)
            EXECUTE FUNCTION notify_email_campaign_event();
            """
        )
        op.execute(
            """
            CREATE TRIGGER email_campaign_recipient_status_event
            AFTER UPDATE OF status ON email_campaign_recipients
            FOR EACH ROW
            WHEN (OLD.status IS DISTINCT FROM NEW.status)
            EXECUTE FUNCTION notify_email_campaign_event();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS email_campaign_recipient_status_event "
            "ON email_campaign_recipients"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS email_campaign_status_event ON email_campaigns"
        )
        op.execute("DROP FUNCTION IF EXISTS notify_email_campaign_event()")

    op.drop_index(
        "ix_email_campaign_recipients_campaign_status",
        table_name="email_campaign_recipients",
    )
    op.drop_index(
        "ix_email_campaign_recipients_due",
        table_name="email_campaign_recipients",
    )
    op.drop_index(
        "ix_email_campaign_recipients_scheduled_at",
        table_name="email_campaign_recipients",
    )
    op.drop_index(
        "ix_email_campaign_recipients_gmail_account_id",
        table_name="email_campaign_recipients",
    )
    op.drop_index(
        "ix_email_campaign_recipients_contact_id",
        table_name="email_campaign_recipients",
    )
    op.drop_index(
        "ix_email_campaign_recipients_company_id",
        table_name="email_campaign_recipients",
    )
    op.drop_index(
        "ix_email_campaign_recipients_campaign_id",
        table_name="email_campaign_recipients",
    )
    op.drop_index(
        "ix_email_campaign_recipients_org_id",
        table_name="email_campaign_recipients",
    )
    op.drop_table("email_campaign_recipients")

    op.drop_index("ix_email_campaigns_template_id", table_name="email_campaigns")
    op.drop_index("ix_email_campaigns_created_by", table_name="email_campaigns")
    op.drop_index("ix_email_campaigns_org_id", table_name="email_campaigns")
    op.drop_table("email_campaigns")
