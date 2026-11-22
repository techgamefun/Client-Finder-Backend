import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import SessionLocal
from app.models import (
    ConnectedGmailAccount,
    EmailCampaign,
    EmailCampaignDelivery,
    EmailCampaignRecipient,
    User,
    UserGmailAssignment,
    OrganizationMembership,
)
from app.services.campaign_service import maybe_complete_campaign
from app.services.email_suppression import add_suppression, normalize_email
from app.services.gmail import (
    _build_gmail_service,
    _execute_google_request,
    _extract_message_body,
    _message_header,
)
from app.tenant_context import TenantContext, reset_tenant_context, set_tenant_context


logger = logging.getLogger(__name__)
_EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_HARD_BOUNCE_TERMS = (
    "delivery status notification",
    "delivery failed",
    "undelivered mail",
    "recipient address rejected",
    "user unknown",
    "permanent failure",
    "5.1.0",
    "5.1.1",
    "5.2.1",
)
BOUNCE_LOOKBACK_DAYS = 30


def _claim_monitor_account() -> tuple[UUID, TenantContext] | None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=settings.email_monitor_interval_seconds)
    with SessionLocal() as db:
        account = db.scalar(
            select(ConnectedGmailAccount)
            .where(
                or_(
                    ConnectedGmailAccount.last_monitor_at.is_(None),
                    ConnectedGmailAccount.last_monitor_at <= cutoff,
                )
            )
            .order_by(ConnectedGmailAccount.last_monitor_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if account is None:
            db.commit()
            return None
        assignment = db.execute(
            select(UserGmailAssignment, User, OrganizationMembership)
            .join(User, User.id == UserGmailAssignment.user_id)
            .join(
                OrganizationMembership,
                OrganizationMembership.user_id == User.id,
            )
            .where(
                UserGmailAssignment.gmail_account_id == account.id,
                UserGmailAssignment.org_id == account.org_id,
                OrganizationMembership.organization_id == account.org_id,
                OrganizationMembership.is_active.is_(True),
                User.is_active.is_(True),
            )
            .order_by(OrganizationMembership.role.desc(), User.email)
            .limit(1)
        ).first()
        if assignment is None:
            account.last_monitor_at = now
            account.last_monitor_error = "No active user is assigned to this Gmail account."
            db.commit()
            return None
        _, user, membership = assignment
        account.last_monitor_at = now
        account.last_monitor_error = None
        delivery = (
            account.id,
            TenantContext(
                organization_id=account.org_id,
                user_id=user.id,
                email=user.email,
                role=membership.role,
            ),
        )
        db.commit()
        return delivery


def _mark_monitor_error(account_id: UUID, detail: str) -> None:
    with SessionLocal() as db:
        account = db.get(ConnectedGmailAccount, account_id)
        if account is not None:
            account.last_monitor_error = detail[:1000]
            db.commit()


def _monitor_account(account_id: UUID, context: TenantContext) -> None:
    tenant_token = set_tenant_context(context)
    try:
        with SessionLocal() as db:
            db.info["tenant_context"] = context
            account = db.get(ConnectedGmailAccount, account_id)
            if account is None:
                return
            service = _build_gmail_service(account)
            _detect_replies(db, service, account_id, context.organization_id)
            _detect_hard_bounces(db, service, account_id, context.organization_id)
            account.last_monitor_error = None
            db.commit()
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        logger.warning("Gmail campaign monitor failed for %s: %s", account_id, detail)
        _mark_monitor_error(account_id, detail)
    except Exception:
        logger.exception("Gmail campaign monitor failed for account %s.", account_id)
        _mark_monitor_error(account_id, "Gmail activity could not be checked.")
    finally:
        reset_tenant_context(tenant_token)


def _detect_replies(db, service, account_id: UUID, organization_id: UUID) -> None:
    recent_completed_cutoff = datetime.now(timezone.utc) - timedelta(
        days=BOUNCE_LOOKBACK_DAYS
    )
    check_cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.email_monitor_interval_seconds
    )
    recipients = db.scalars(
        select(EmailCampaignRecipient)
        .join(
            EmailCampaign,
            EmailCampaign.id == EmailCampaignRecipient.campaign_id,
        )
        .where(
            EmailCampaignRecipient.org_id == organization_id,
            EmailCampaignRecipient.gmail_account_id == account_id,
            or_(
                EmailCampaignRecipient.status == "pending",
                and_(
                    EmailCampaignRecipient.status == "sent",
                    EmailCampaignRecipient.sent_at >= recent_completed_cutoff,
                ),
            ),
            EmailCampaignRecipient.current_step > 0,
            EmailCampaignRecipient.gmail_thread_id.is_not(None),
            or_(
                EmailCampaignRecipient.reply_checked_at.is_(None),
                EmailCampaignRecipient.reply_checked_at <= check_cutoff,
            ),
        )
        .order_by(
            EmailCampaignRecipient.reply_checked_at.asc().nulls_first(),
            EmailCampaignRecipient.id,
        )
        .limit(500)
    ).all()
    recipient_ids = [recipient.id for recipient in recipients]
    deliveries_by_recipient: dict[UUID, list[EmailCampaignDelivery]] = {
        recipient_id: [] for recipient_id in recipient_ids
    }
    if recipient_ids:
        deliveries = db.scalars(
            select(EmailCampaignDelivery)
            .where(
                EmailCampaignDelivery.recipient_id.in_(recipient_ids),
                EmailCampaignDelivery.gmail_account_id == account_id,
            )
            .order_by(EmailCampaignDelivery.step_index)
        ).all()
        for delivery in deliveries:
            deliveries_by_recipient[delivery.recipient_id].append(delivery)

    for recipient in recipients:
        now = datetime.now(timezone.utc)
        recipient_email = normalize_email(recipient.to_email)
        found_reply = False
        for delivery in deliveries_by_recipient[recipient.id]:
            try:
                thread = _execute_google_request(
                    service.users()
                    .threads()
                    .get(
                        userId="me",
                        id=delivery.gmail_thread_id,
                        format="full",
                    ),
                    missing_message_detail=(
                        "The campaign Gmail thread was not found."
                    ),
                )
            except HTTPException as exc:
                if exc.status_code == 404:
                    continue
                raise
            messages = thread.get("messages")
            if not isinstance(messages, list):
                continue
            sent_at = delivery.sent_at
            sent_at = (
                sent_at.replace(tzinfo=timezone.utc)
                if sent_at.tzinfo is None
                else sent_at
            )
            for message in messages:
                if not isinstance(message, dict):
                    continue
                raw_date = message.get("internalDate")
                try:
                    received_at = datetime.fromtimestamp(
                        int(raw_date) / 1000,
                        tz=timezone.utc,
                    )
                except (TypeError, ValueError, OSError):
                    continue
                if received_at <= sent_at:
                    continue
                senders = [
                    address
                    for _, address in getaddresses(
                        [_message_header(message, "From")]
                    )
                    if address
                ]
                if not any(
                    _is_recipient_address(sender, recipient_email)
                    for sender in senders
                ):
                    continue
                recipient.status = "replied"
                recipient.replied_at = received_at
                recipient.updated_at = now
                maybe_complete_campaign(db, recipient.campaign_id, now=now)
                db.flush()
                found_reply = True
                break
            if found_reply:
                break
        if not found_reply:
            recipient.reply_checked_at = now
            db.flush()


def _detect_hard_bounces(
    db,
    service,
    account_id: UUID,
    organization_id: UUID,
) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=BOUNCE_LOOKBACK_DAYS)
    deliveries = db.execute(
        select(EmailCampaignDelivery.to_email, EmailCampaignRecipient)
        .join(
            EmailCampaignRecipient,
            EmailCampaignRecipient.id == EmailCampaignDelivery.recipient_id,
        )
        .where(
            EmailCampaignDelivery.org_id == organization_id,
            EmailCampaignDelivery.gmail_account_id == account_id,
            EmailCampaignDelivery.sent_at >= since,
            EmailCampaignRecipient.status.in_(("pending", "sent")),
        )
    ).all()
    tracked: dict[str, list[EmailCampaignRecipient]] = {}
    for email, recipient in deliveries:
        tracked.setdefault(normalize_email(email), []).append(recipient)
    if not tracked:
        return

    messages = _execute_google_request(
        service.users()
        .messages()
        .list(
            userId="me",
            q="newer_than:30d {from:mailer-daemon from:postmaster}",
            maxResults=50,
        ),
        missing_message_detail="Gmail bounce notices could not be loaded.",
    ).get("messages", [])
    if not isinstance(messages, list):
        return

    for item in messages:
        if not isinstance(item, dict):
            continue
        message_id = item.get("id")
        if not isinstance(message_id, str) or not message_id:
            continue
        message = _execute_google_request(
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full"),
            missing_message_detail="A Gmail bounce notice was not found.",
        )
        body = _extract_message_body(message.get("payload", {})).casefold()
        subject = _message_header(message, "Subject").casefold()
        report_text = f"{subject}\n{body}"
        if not any(term in report_text for term in _HARD_BOUNCE_TERMS):
            continue
        reported_addresses = {
            _normalized_address(match)
            for match in _EMAIL_PATTERN.findall(report_text)
        }
        for email in reported_addresses.intersection(tracked):
            for recipient in tracked[email]:
                if recipient.status not in {"pending", "sent"}:
                    continue
                now = datetime.now(timezone.utc)
                recipient.status = "bounced"
                recipient.bounced_at = now
                recipient.error_message = "A hard-bounce notification was received."
                recipient.updated_at = now
                add_suppression(
                    db,
                    organization_id=organization_id,
                    target_type="email",
                    value=email,
                    reason="hard_bounce",
                )
                maybe_complete_campaign(db, recipient.campaign_id, now=now)
                db.flush()


def _normalized_address(value: str) -> str:
    try:
        return normalize_email(value)
    except HTTPException:
        return value.casefold()


def _is_recipient_address(value: str, expected: str) -> bool:
    try:
        return normalize_email(value) == expected
    except HTTPException:
        return False


async def process_due_gmail_monitoring() -> bool:
    try:
        delivery = await run_in_threadpool(_claim_monitor_account)
    except Exception:
        logger.exception("Could not claim a Gmail account for monitoring.")
        return False
    if delivery is None:
        return False
    account_id, context = delivery
    await run_in_threadpool(_monitor_account, account_id, context)
    return True
