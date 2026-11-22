
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    select,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
)
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship, with_loader_criteria

from app.database import Base
from app.tenant_context import get_tenant_context


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    logo_url: Mapped[str | None] = mapped_column(String(2048))
    banner_url: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    auth_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    profile_photo_url: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'user')",
            name="ck_users_role_allowed",
        ),
    )


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_organization_memberships_org_user",
        ),
        CheckConstraint(
            "role IN ('admin', 'user')",
            name="ck_organization_memberships_role_allowed",
        ),
    )


class OrganizationInvitation(Base):
    __tablename__ = "organization_invitations"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )
    invited_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'user')",
            name="ck_organization_invitations_role_allowed",
        ),
        Index(
            "ix_organization_invitations_org_email",
            "organization_id",
            "email",
        ),
    )


class AuthRefreshSession(Base):
    __tablename__ = "auth_refresh_sessions"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(String(256), nullable=False)
    auth: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_push_subscriptions_endpoint"),
    )


class UserPushPreferences(Base):
    __tablename__ = "user_push_preferences"

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    website_crawl_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    pagespeed_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    outreach_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UserPreferences(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UserProviderCredential(Base):
    __tablename__ = "user_provider_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_provider_credential"),
    )


class ConnectedGmailAccount(Base):
    __tablename__ = "connected_gmail_accounts"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    org_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    email_address: Mapped[str] = mapped_column(String(320), nullable=False)
    google_account_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    last_monitor_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    last_monitor_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "google_account_id",
            name="uq_connected_gmail_google_account_id",
        ),
    )


class UserGmailAssignment(Base):
    __tablename__ = "user_gmail_assignments"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    org_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gmail_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("connected_gmail_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assigned_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "gmail_account_id",
            name="uq_user_gmail_assignment",
        ),
    )


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    org_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    body_content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SuppressedContact(Base):
    __tablename__ = "suppressed_contacts"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    global_dnc: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "target_type IN ('email', 'domain')",
            name="ck_suppressed_contacts_target_type",
        ),
        CheckConstraint(
            "reason IN ('unsubscribe', 'hard_bounce', 'complaint', 'manual_dnc')",
            name="ck_suppressed_contacts_reason",
        ),
        UniqueConstraint(
            "organization_id",
            "target_type",
            "normalized_value",
            "reason",
            name="uq_suppressed_contacts_entry",
        ),
        Index(
            "ix_suppressed_contacts_lookup",
            "organization_id",
            "target_type",
            "normalized_value",
        ),
        Index(
            "ix_suppressed_contacts_global_lookup",
            "global_dnc",
            "target_type",
            "normalized_value",
        ),
    )


class EmailCampaign(Base):
    __tablename__ = "email_campaigns"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    org_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    template_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("email_templates.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    follow_up_steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )
    verify_mx: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    gmail_account_ids: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )
    daily_limit_per_account: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=40,
        server_default="40",
    )
    min_delay_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=180,
        server_default="180",
    )
    max_delay_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=420,
        server_default="420",
    )
    send_window_start_hour: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=9,
        server_default="9",
    )
    send_window_end_hour: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=17,
        server_default="17",
    )
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="UTC",
        server_default="UTC",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="draft",
        server_default="draft",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'running', 'paused', 'completed')",
            name="ck_email_campaigns_status_allowed",
        ),
        CheckConstraint(
            "daily_limit_per_account > 0",
            name="ck_email_campaigns_daily_limit_positive",
        ),
        CheckConstraint(
            "min_delay_seconds >= 1 AND max_delay_seconds >= min_delay_seconds",
            name="ck_email_campaigns_delay_range",
        ),
        CheckConstraint(
            "send_window_start_hour >= 0 AND send_window_start_hour < send_window_end_hour "
            "AND send_window_end_hour <= 23",
            name="ck_email_campaigns_send_window",
        ),
    )


class EmailCampaignRecipient(Base):
    __tablename__ = "email_campaign_recipients"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    org_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    campaign_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("email_campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gmail_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("connected_gmail_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    to_email: Mapped[str] = mapped_column(String(320), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    gmail_message_id: Mapped[str | None] = mapped_column(String(255))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_step: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    sent_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reply_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_verification_status: Mapped[str | None] = mapped_column(String(20))
    email_verification_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'failed', 'cancelled', "
            "'replied', 'bounced')",
            name="ck_email_campaign_recipients_status_allowed",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_email_campaign_recipients_attempts_nonnegative",
        ),
        CheckConstraint(
            "current_step >= 0 AND sent_count >= 0",
            name="ck_email_campaign_recipients_sequence_counters_nonnegative",
        ),
        UniqueConstraint(
            "campaign_id",
            "contact_id",
            name="uq_email_campaign_recipient_contact",
        ),
        Index(
            "ix_email_campaign_recipients_due",
            "status",
            "scheduled_at",
        ),
        Index(
            "ix_email_campaign_recipients_campaign_status",
            "campaign_id",
            "status",
        ),
        Index(
            "ix_email_campaign_recipients_org_status_sent_at",
            "org_id",
            "status",
            "sent_at",
        ),
        Index(
            "ix_email_campaign_recipients_reply_monitor",
            "gmail_account_id",
            "status",
            "reply_checked_at",
        ),
    )


class TenantOwnedMixin:
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )


website_search_keywords = Table(
    "website_search_keywords",
    Base.metadata,
    Column(
        "website_id",
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "search_keyword_id",
        Uuid(as_uuid=True),
        ForeignKey("search_keywords.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

pagespeed_scan_job_companies = Table(
    "pagespeed_scan_job_companies",
    Base.metadata,
    Column(
        "job_id",
        Uuid(as_uuid=True),
        ForeignKey("pagespeed_scan_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "company_id",
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class EmailCampaignDelivery(Base):
    __tablename__ = "email_campaign_deliveries"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    org_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    campaign_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("email_campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recipient_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("email_campaign_recipients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gmail_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("connected_gmail_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    to_email: Mapped[str] = mapped_column(String(320), nullable=False)
    gmail_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    gmail_thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "recipient_id",
            "step_index",
            name="uq_email_campaign_delivery_recipient_step",
        ),
        CheckConstraint(
            "step_index >= 0",
            name="ck_email_campaign_deliveries_step_nonnegative",
        ),
        Index(
            "ix_email_campaign_deliveries_account_sent",
            "gmail_account_id",
            "sent_at",
        ),
    )


class Company(TenantOwnedMixin, Base):
    __tablename__ = "companies"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str | None] = mapped_column(
        String(200),
        index=True,
    )
    website: Mapped[str | None] = mapped_column(String(500))
    domain: Mapped[str | None] = mapped_column(
        String(255),
    )
    industry: Mapped[str | None] = mapped_column(String(150))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))

    software_need: Mapped[str | None] = mapped_column(Text)
    lead_score: Mapped[int | None] = mapped_column(Integer)
    lead_status: Mapped[str] = mapped_column(
        String(30),
        default="new",
        server_default="new",
        nullable=False,
    )
    last_email_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )
    created_by_user: Mapped["User | None"] = relationship(
        foreign_keys="Company.created_by_user_id",
    )

    pagespeed_scans: Mapped[list["PageSpeedScan"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
        order_by="desc(PageSpeedScan.scanned_at)",
    )

    @property
    def created_by_email(self) -> str | None:
        return (
            self.created_by_user.email
            if self.created_by_user is not None
            else None
        )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "domain",
            name="uq_companies_organization_domain",
        ),
        Index("ix_companies_name", "name"),
        Index("ix_companies_industry", "industry"),
        Index(
            "ix_companies_organization_created_at",
            "organization_id",
            "created_at",
        ),
    )


class Contact(TenantOwnedMixin, Base):
    __tablename__ = "contacts"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    company_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(150))
    business_email: Mapped[str | None] = mapped_column(String(320))
    business_phone: Mapped[str | None] = mapped_column(String(50))

    is_publicly_listed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )

    company: Mapped["Company"] = relationship(
        back_populates="contacts",
    )

    __table_args__ = (
        Index(
            "ix_contacts_company_organization",
            "company_id",
            "organization_id",
        ),
    )


class SearchKeyword(TenantOwnedMixin, Base):
    __tablename__ = "search_keywords"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    keyword: Mapped[str] = mapped_column(String(300), nullable=False)
    searched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    websites: Mapped[list["Website"]] = relationship(
        secondary=website_search_keywords,
        back_populates="search_keywords",
        order_by="desc(SearchKeyword.searched_at)",
    )


class DiscoveryProviderJob(TenantOwnedMixin, Base):
    __tablename__ = "discovery_provider_jobs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    search_keyword_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("search_keywords.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_task_id: Mapped[str] = mapped_column(String(255), nullable=False)
    lot_no: Mapped[str | None] = mapped_column(String(255))
    max_results: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_mapping: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    results: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    warning: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class WebsiteCrawl(TenantOwnedMixin, Base):
    __tablename__ = "website_crawls"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    website_url: Mapped[str] = mapped_column(
        String(2000),
        nullable=False,
    )
    site_type: Mapped[str | None] = mapped_column(String(64))
    home_page_url: Mapped[str | None] = mapped_column(String(2000))
    home_content: Mapped[str | None] = mapped_column(Text)
    about_page_url: Mapped[str | None] = mapped_column(String(2000))
    about_content: Mapped[str | None] = mapped_column(Text)
    blog_page_url: Mapped[str | None] = mapped_column(String(2000))
    blog_content: Mapped[str | None] = mapped_column(Text)
    service_page_url: Mapped[str | None] = mapped_column(String(2000))
    service_content: Mapped[str | None] = mapped_column(Text)
    contact_page_url: Mapped[str | None] = mapped_column(String(2000))
    contact_content: Mapped[str | None] = mapped_column(Text)
    goals_page_url: Mapped[str | None] = mapped_column(String(2000))
    goals_content: Mapped[str | None] = mapped_column(Text)
    classification_signals: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    emails: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    phones: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    social_links: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    crawled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    websites: Mapped[list["Website"]] = relationship(
        back_populates="crawl",
        passive_deletes=True,
    )
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "website_url",
            name="uq_website_crawls_organization_url",
        ),
    )


class WebsiteCorpusAnalysis(TenantOwnedMixin, Base):
    __tablename__ = "website_corpus_analyses"

    website_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="CASCADE"),
        primary_key=True,
    )
    words: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    algorithm_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    source_crawled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class WebsiteCorpusAnalysisJob(TenantOwnedMixin, Base):
    __tablename__ = "website_corpus_analysis_jobs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    website_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_crawled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="queued",
        server_default="queued",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    website: Mapped["Website"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "website_id",
            "source_crawled_at",
            name="uq_website_corpus_analysis_jobs_website_crawl",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'skipped')",
            name="ck_website_corpus_analysis_jobs_status_allowed",
        ),
        Index(
            "ix_website_corpus_analysis_jobs_status_created_at",
            "status",
            "created_at",
        ),
        Index(
            "ix_website_corpus_analysis_jobs_organization_created_at",
            "organization_id",
            "created_at",
        ),
        Index(
            "ix_website_corpus_analysis_jobs_website_created_at",
            "website_id",
            "created_at",
        ),
    )


class Website(TenantOwnedMixin, Base):
    __tablename__ = "websites"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    website_url: Mapped[str] = mapped_column(
        String(2000),
        nullable=False,
    )
    domain: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    favicon_url: Mapped[str | None] = mapped_column(String(2000))
    crawl_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("website_crawls.id", ondelete="SET NULL"),
    )
    category: Mapped[str | None] = mapped_column(String(64))
    page_type: Mapped[str | None] = mapped_column(String(64))
    classification_manual: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    site_topic: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    language: Mapped[str | None] = mapped_column(String(35))
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="new",
        server_default="new",
    )
    discovery_source: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="manual",
        server_default="manual",
    )
    discovery_provider: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    crawl: Mapped[WebsiteCrawl | None] = relationship(back_populates="websites")
    created_by_user: Mapped["User | None"] = relationship(
        foreign_keys="Website.created_by_user_id",
    )
    hypestat_profile: Mapped["WebsiteHypeStatProfile | None"] = relationship(
        back_populates="website",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )
    search_keywords: Mapped[list[SearchKeyword]] = relationship(
        secondary=website_search_keywords,
        back_populates="websites",
        order_by="desc(SearchKeyword.searched_at)",
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "website_url",
            name="uq_websites_organization_url",
        ),
        UniqueConstraint(
            "organization_id",
            "domain",
            name="uq_websites_organization_domain",
        ),
        CheckConstraint(
            "status IN ('new', 'lead', 'checked')",
            name="ck_websites_status_allowed",
        ),
        Index("ix_websites_page_type", "page_type"),
        Index("ix_websites_created_at", "created_at"),
        Index(
            "ix_websites_organization_created_at",
            "organization_id",
            "created_at",
        ),
    )


class WebsiteHypeStatProfile(TenantOwnedMixin, Base):
    __tablename__ = "website_hypestat_profiles"

    website_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    website: Mapped[Website] = relationship(back_populates="hypestat_profile")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'queued', 'failed', 'invalid_domain', 'removed')",
            name="ck_website_hypestat_profiles_status_allowed",
        ),
    )


class WebsiteCrawlJob(TenantOwnedMixin, Base):
    __tablename__ = "website_crawl_jobs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    website_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="queued",
        server_default="queued",
        index=True,
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error: Mapped[str | None] = mapped_column(Text)
    skip_reason: Mapped[str | None] = mapped_column(Text)
    crawl_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("website_crawls.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    crawl: Mapped[WebsiteCrawl | None] = relationship()

    __table_args__ = (
        Index(
            "ix_website_crawl_jobs_organization_created_at",
            "organization_id",
            "created_at",
        ),
        Index(
            "ix_website_crawl_jobs_organization_completed_at",
            "organization_id",
            "completed_at",
        ),
    )


class ExcludedDomain(TenantOwnedMixin, Base):
    __tablename__ = "excluded_domains"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="user",
        server_default="user",
    )
    failure_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "domain",
            name="uq_excluded_domains_organization_domain",
        ),
        CheckConstraint(
            "reason IN ('Big Tech / Social', 'Manual Exclusion', "
            "'Unreachable', 'Blocked (403/429)')",
            name="ck_excluded_domains_reason_allowed",
        ),
        CheckConstraint(
            "source IN ('system_preset', 'user', 'auto_crawler')",
            name="ck_excluded_domains_source_allowed",
        ),
        CheckConstraint(
            "failure_count >= 0",
            name="ck_excluded_domains_failure_count_nonnegative",
        ),
    )


class CrawlerDomainFailure(TenantOwnedMixin, Base):
    __tablename__ = "crawler_domain_failures"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    failure_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "domain",
            name="uq_crawler_domain_failures_organization_domain",
        ),
    )


class ApplicationSettings(TenantOwnedMixin, Base):
    __tablename__ = "application_settings"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    hypestat_auto_fetch_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    selected_discovery_tools: Mapped[list[str] | None] = mapped_column(JSON)
    tavily_default_search_depth: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="basic",
        server_default="basic",
    )
    tavily_default_country: Mapped[str | None] = mapped_column(String(100))
    tavily_default_exclude_domains: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    octoparse_template: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    crawl_max_pages: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=6,
        server_default="6",
    )
    crawl_max_depth: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=2,
        server_default="2",
    )
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            name="uq_application_settings_organization",
        ),
    )


class PageSpeedScan(TenantOwnedMixin, Base):
    __tablename__ = "pagespeed_scans"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    company_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=True,
    )
    website_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "websites.id",
            ondelete="CASCADE",
            name="fk_pagespeed_scans_website_id_websites",
        ),
    )
    website_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    strategy: Mapped[str] = mapped_column(String(10), nullable=False)
    category_scores: Mapped[dict[str, int | None]] = mapped_column(
        JSON,
        nullable=False,
    )
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    company: Mapped["Company | None"] = relationship(
        back_populates="pagespeed_scans",
    )
    website: Mapped["Website | None"] = relationship()

    __table_args__ = (
        CheckConstraint(
            "company_id IS NOT NULL OR website_id IS NOT NULL",
            name="ck_pagespeed_scans_has_owner",
        ),
        Index(
            "ix_pagespeed_scans_company_scanned_at",
            "company_id",
            "scanned_at",
        ),
        Index(
            "ix_pagespeed_scans_website_scanned_at",
            "website_id",
            "scanned_at",
        ),
    )


class PageSpeedScanJob(TenantOwnedMixin, Base):
    __tablename__ = "pagespeed_scan_jobs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    company_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=True,
    )
    website_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "websites.id",
            ondelete="CASCADE",
            name="fk_pagespeed_scan_jobs_website_id_websites",
        ),
    )
    website_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    strategy: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="queued",
        server_default="queued",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error: Mapped[str | None] = mapped_column(Text)
    scan_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("pagespeed_scans.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    companies: Mapped[list[Company]] = relationship(
        secondary=pagespeed_scan_job_companies,
    )
    website: Mapped["Website | None"] = relationship()
    scan: Mapped[PageSpeedScan | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "company_id IS NOT NULL OR website_id IS NOT NULL",
            name="ck_pagespeed_scan_jobs_has_owner",
        ),
        Index(
            "ix_pagespeed_scan_jobs_status",
            "status",
            "created_at",
        ),
        Index(
            "ix_pagespeed_scan_jobs_organization_created_at",
            "organization_id",
            "created_at",
        ),
        Index(
            "ix_pagespeed_scan_jobs_website_created_at",
            "website_id",
            "created_at",
        ),
    )


_TENANT_MODELS = (
    Company,
    Contact,
    SearchKeyword,
    DiscoveryProviderJob,
    WebsiteCrawl,
    WebsiteCorpusAnalysis,
    WebsiteCorpusAnalysisJob,
    Website,
    WebsiteHypeStatProfile,
    WebsiteCrawlJob,
    ExcludedDomain,
    CrawlerDomainFailure,
    ApplicationSettings,
    PageSpeedScan,
    PageSpeedScanJob,
    PushSubscription,
    UserPushPreferences,
    UserPreferences,
)

_ORG_ID_MODELS = (
    ConnectedGmailAccount,
    UserGmailAssignment,
    EmailTemplate,
)


@event.listens_for(Session, "do_orm_execute")
def _scope_tenant_queries(execute_state: Any) -> None:
    context = get_tenant_context() or execute_state.session.info.get(
        "tenant_context"
    )
    if context is None:
        return

    statement = execute_state.statement
    if execute_state.is_select:
        organization_id = context.organization_id
        statement = statement.options(
            with_loader_criteria(
                User,
                lambda entity: entity.id.in_(
                    select(OrganizationMembership.user_id).where(
                        OrganizationMembership.organization_id
                        == organization_id,
                        OrganizationMembership.is_active.is_(True),
                    )
                ),
                include_aliases=True,
            )
        )
        for model in _TENANT_MODELS:
            organization_id = context.organization_id
            statement = statement.options(
                with_loader_criteria(
                    model,
                    lambda entity: entity.organization_id == organization_id,
                    include_aliases=True,
                )
            )
        for model in _ORG_ID_MODELS:
            organization_id = context.organization_id
            statement = statement.options(
                with_loader_criteria(
                    model,
                    lambda entity: entity.org_id == organization_id,
                    include_aliases=True,
                )
            )
        execute_state.statement = statement
        return

    mapper = execute_state.bind_arguments.get("mapper")
    model = mapper.class_ if mapper is not None else None
    if model is User:
        execute_state.statement = statement.where(
            model.id.in_(
                select(OrganizationMembership.user_id).where(
                    OrganizationMembership.organization_id
                    == context.organization_id,
                    OrganizationMembership.is_active.is_(True),
                )
            )
        )
    elif model in _TENANT_MODELS:
        execute_state.statement = statement.where(
            model.organization_id == context.organization_id
        )
    elif model in _ORG_ID_MODELS:
        execute_state.statement = statement.where(
            model.org_id == context.organization_id
        )


@event.listens_for(Session, "before_flush")
def _assign_tenant_to_new_entities(
    session: Session,
    flush_context: Any,
    instances: Any,
) -> None:
    context = get_tenant_context() or session.info.get("tenant_context")
    for instance in session.new:
        if not isinstance(instance, TenantOwnedMixin):
            continue
        if context is None:
            raise RuntimeError("Tenant context is required to save business data.")
        if (
            instance.organization_id is not None
            and instance.organization_id != context.organization_id
        ):
            raise RuntimeError("Cannot create data for another organization.")
        instance.organization_id = context.organization_id
        if instance.created_by_user_id is None and context.user_id is not None:
            instance.created_by_user_id = context.user_id
    for instance in session.new:
        if not isinstance(instance, _ORG_ID_MODELS):
            continue
        if context is None:
            raise RuntimeError("Tenant context is required to save Gmail data.")
        if instance.org_id is not None and instance.org_id != context.organization_id:
            raise RuntimeError("Cannot create Gmail data for another organization.")
        instance.org_id = context.organization_id

    if context is not None:
        for instance in session.dirty:
            state = inspect(instance)
            if isinstance(instance, TenantOwnedMixin) and (
                state.attrs.organization_id.history.has_changes()
            ):
                raise RuntimeError("Organization ownership cannot be changed.")
            if (
                isinstance(instance, _ORG_ID_MODELS)
                and state.attrs.org_id.history.has_changes()
            ):
                raise RuntimeError("Gmail organization ownership cannot be changed.")
