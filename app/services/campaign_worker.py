import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, update
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.models import (
    Company,
    EmailCampaign,
    EmailCampaignRecipient,
    EmailTemplate,
    User,
)
from app.services.campaign_service import maybe_complete_campaign, utc_now
from app.services.email_templates import (
    build_lead_context,
    build_sender_info,
    render_email_template,
)
from app.services.gmail import get_gmail_account_email, send_gmail_message
from app.tenant_context import TenantContext, reset_tenant_context, set_tenant_context


logger = logging.getLogger(__name__)
RECIPIENT_LEASE_SECONDS = 900


@dataclass(frozen=True)
class CampaignRecipientDelivery:
    recipient_id: UUID
    context: TenantContext


def _claim_due_campaign_recipient() -> CampaignRecipientDelivery | None:
    now = utc_now()
    lease_cutoff = now - timedelta(seconds=RECIPIENT_LEASE_SECONDS)
    with SessionLocal() as db:
        stale_campaign_ids = list(
            db.scalars(
                select(EmailCampaignRecipient.campaign_id)
                .where(
                    EmailCampaignRecipient.status == "processing",
                    EmailCampaignRecipient.updated_at < lease_cutoff,
                )
                .distinct()
            ).all()
        )
        if stale_campaign_ids:
            db.execute(
                update(EmailCampaignRecipient)
                .where(
                    EmailCampaignRecipient.status == "processing",
                    EmailCampaignRecipient.updated_at < lease_cutoff,
                )
                .values(
                    status="failed",
                    error_message=(
                        "The worker stopped while sending this email; "
                        "delivery status is unknown."
                    ),
                    updated_at=now,
                )
            )
            for campaign_id in stale_campaign_ids:
                maybe_complete_campaign(db, campaign_id, now=now)

        active_recipients = (
            select(EmailCampaignRecipient.id)
            .where(
                EmailCampaignRecipient.campaign_id == EmailCampaign.id,
                EmailCampaignRecipient.status.in_(("pending", "processing")),
            )
            .exists()
        )
        db.execute(
            update(EmailCampaign)
            .where(
                EmailCampaign.status == "running",
                ~active_recipients,
            )
            .values(status="completed", updated_at=now)
        )

        recipient = db.scalar(
            select(EmailCampaignRecipient)
            .join(
                EmailCampaign,
                EmailCampaign.id == EmailCampaignRecipient.campaign_id,
            )
            .where(
                EmailCampaignRecipient.status == "pending",
                EmailCampaignRecipient.scheduled_at <= now,
                EmailCampaign.status == "running",
            )
            .order_by(
                EmailCampaignRecipient.scheduled_at,
                EmailCampaignRecipient.id,
            )
            .with_for_update(
                skip_locked=True,
                of=EmailCampaignRecipient,
            )
            .limit(1)
        )
        if recipient is None:
            db.commit()
            return None

        campaign = db.get(EmailCampaign, recipient.campaign_id)
        user = (
            db.scalar(
                select(User).where(
                    User.id == campaign.created_by,
                    User.organization_id == campaign.org_id,
                )
            )
            if campaign is not None
            else None
        )
        if campaign is None or user is None:
            recipient.status = "failed"
            recipient.error_message = (
                "The campaign owner or campaign could not be loaded."
            )
            recipient.updated_at = now
            if campaign is not None:
                maybe_complete_campaign(db, campaign.id, now=now)
            db.commit()
            return None

        recipient.status = "processing"
        recipient.attempts += 1
        recipient.updated_at = now
        delivery = CampaignRecipientDelivery(
            recipient_id=recipient.id,
            context=TenantContext(
                organization_id=campaign.org_id,
                user_id=user.id,
                email=user.email,
                role=user.role,
            ),
        )
        db.commit()
        return delivery


def _mark_campaign_recipient_failed(
    recipient_id: UUID,
    detail: str,
) -> None:
    now = utc_now()
    with SessionLocal() as db:
        recipient = db.get(EmailCampaignRecipient, recipient_id)
        if recipient is None or recipient.status != "processing":
            return
        recipient.status = "failed"
        recipient.error_message = detail
        recipient.updated_at = now
        maybe_complete_campaign(db, recipient.campaign_id, now=now)
        db.commit()


def _deliver_campaign_recipient(delivery: CampaignRecipientDelivery) -> None:
    tenant_token = set_tenant_context(delivery.context)
    try:
        with SessionLocal() as db:
            db.info["tenant_context"] = delivery.context
            recipient = db.get(EmailCampaignRecipient, delivery.recipient_id)
            if recipient is None or recipient.status != "processing":
                return

            campaign = db.get(EmailCampaign, recipient.campaign_id)
            if campaign is None or campaign.status != "running":
                recipient.status = (
                    "pending" if campaign is not None and campaign.status == "paused"
                    else "cancelled"
                )
                recipient.updated_at = utc_now()
                if campaign is not None:
                    maybe_complete_campaign(db, campaign.id)
                db.commit()
                return

            user = db.scalar(
                select(User).where(
                    User.id == campaign.created_by,
                    User.organization_id == campaign.org_id,
                )
            )
            template = db.scalar(
                select(EmailTemplate).where(
                    EmailTemplate.id == campaign.template_id,
                    EmailTemplate.org_id == campaign.org_id,
                )
            )
            if user is None or not user.is_active:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="The campaign owner is no longer active.",
                )
            if template is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="The campaign email template no longer exists.",
                )

            sender_email = get_gmail_account_email(
                db,
                recipient.gmail_account_id,
            )
            sender_info = build_sender_info(
                db,
                organization_id=campaign.org_id,
                user_email=user.email,
                role=user.role,
                email=sender_email,
            )
            lead_context = build_lead_context(
                db,
                recipient.company_id,
                recipient.contact_id,
                sender_info,
            )
            recipient_email = lead_context["contact"]["email"]
            if not isinstance(recipient_email, str) or not recipient_email.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="The selected contact no longer has a business email address.",
                )
            subject, body_content = render_email_template(
                template.subject,
                template.body_content,
                lead_context,
            )
            sent = send_gmail_message(
                db,
                recipient.gmail_account_id,
                recipient_email=recipient_email,
                subject=subject,
                body_content=body_content,
            )
            now = utc_now()
            recipient.to_email = recipient_email.strip()
            recipient.status = "sent"
            recipient.gmail_message_id = sent.message_id
            recipient.gmail_thread_id = sent.thread_id
            recipient.error_message = None
            recipient.sent_at = now
            recipient.updated_at = now
            company = db.scalar(
                select(Company).where(
                    Company.id == recipient.company_id,
                    Company.organization_id == campaign.org_id,
                )
            )
            if company is not None:
                company.last_email_sent_at = now
            else:
                logger.warning(
                    "Sent campaign recipient %s, but company %s was not found "
                    "in organization %s.",
                    recipient.id,
                    recipient.company_id,
                    campaign.org_id,
                )
            maybe_complete_campaign(db, campaign.id, now=now)
            db.commit()
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        _mark_campaign_recipient_failed(delivery.recipient_id, detail)
    except Exception:
        logger.exception(
            "Email campaign recipient %s failed unexpectedly.",
            delivery.recipient_id,
        )
        _mark_campaign_recipient_failed(
            delivery.recipient_id,
            "The email could not be sent due to an unexpected worker error.",
        )
    finally:
        reset_tenant_context(tenant_token)


async def process_due_campaign_recipient() -> bool:
    try:
        delivery = await run_in_threadpool(_claim_due_campaign_recipient)
    except Exception:
        logger.exception("Could not claim a due email campaign recipient.")
        return False
    if delivery is None:
        return False

    try:
        await run_in_threadpool(_deliver_campaign_recipient, delivery)
    except Exception:
        logger.exception(
            "Could not finalize email campaign recipient %s.",
            delivery.recipient_id,
        )
    return True
