"""Track the latest successful campaign email per company."""

from alembic import op
import sqlalchemy as sa


revision = "e6f4a1b3c9d2"
down_revision = "d39a6f2c8e14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("last_email_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE companies
            SET last_email_sent_at = (
                SELECT MAX(sent_at)
                FROM email_campaign_recipients
                WHERE email_campaign_recipients.company_id = companies.id
                  AND email_campaign_recipients.status = 'sent'
                  AND email_campaign_recipients.sent_at IS NOT NULL
            )
            WHERE EXISTS (
                SELECT 1
                FROM email_campaign_recipients
                WHERE email_campaign_recipients.company_id = companies.id
                  AND email_campaign_recipients.status = 'sent'
                  AND email_campaign_recipients.sent_at IS NOT NULL
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_column("companies", "last_email_sent_at")
