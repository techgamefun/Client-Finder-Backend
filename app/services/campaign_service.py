import re
from datetime import date, datetime, time, timedelta, timezone
from email.headerregistry import Address
from random import randint
from typing import Any, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    Company,
    ConnectedGmailAccount,
    Contact,
    EmailCampaign,
    EmailCampaignRecipient,
    EmailTemplate,
    UserGmailAssignment,
)


_EMAIL_WHITESPACE = re.compile(r"\s")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_campaign_resources(
    db: Session,
    *,
    org_id: UUID,
    user_id: UUID,
    template_id: UUID,
    gmail_account_ids: Sequence[UUID],
) -> None:
    template = db.scalar(
        select(EmailTemplate).where(
            EmailTemplate.id == template_id,
            EmailTemplate.org_id == org_id,
        )
    )
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Email template not found.",
        )

    validate_gmail_account_access(
        db,
        org_id=org_id,
        user_id=user_id,
        gmail_account_ids=gmail_account_ids,
    )


def validate_gmail_account_access(
    db: Session,
    *,
    org_id: UUID,
    user_id: UUID,
    gmail_account_ids: Sequence[UUID],
) -> None:
    for gmail_account_id in gmail_account_ids:
        account = db.scalar(
            select(ConnectedGmailAccount.id).where(
                ConnectedGmailAccount.id == gmail_account_id,
                ConnectedGmailAccount.org_id == org_id,
            )
        )
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Connected Gmail account not found.",
            )
        assignment = db.scalar(
            select(UserGmailAssignment.id).where(
                UserGmailAssignment.org_id == org_id,
                UserGmailAssignment.user_id == user_id,
                UserGmailAssignment.gmail_account_id == gmail_account_id,
            )
        )
        if assignment is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The Gmail account is not assigned to the campaign owner.",
            )


def get_campaign_or_404(
    db: Session,
    *,
    org_id: UUID,
    campaign_id: UUID,
    for_update: bool = False,
) -> EmailCampaign:
    statement = select(EmailCampaign).where(
        EmailCampaign.id == campaign_id,
        EmailCampaign.org_id == org_id,
    )
    if for_update:
        statement = statement.with_for_update()
    campaign = db.scalar(statement)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Email campaign not found.",
        )
    return campaign


def get_eligible_contacts(
    db: Session,
    *,
    org_id: UUID,
    company_ids: Sequence[UUID],
) -> tuple[list[tuple[UUID, UUID, str]], list[UUID]]:
    companies = db.scalars(
        select(Company).where(
            Company.organization_id == org_id,
            Company.id.in_(company_ids),
        )
    ).all()
    if len(companies) != len(company_ids):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or more companies were not found.",
        )

    company_order = {company_id: index for index, company_id in enumerate(company_ids)}
    contacts = db.scalars(
        select(Contact).where(
            Contact.organization_id == org_id,
            Contact.company_id.in_(company_ids),
        )
    ).all()
    contacts.sort(
        key=lambda contact: (
            company_order[contact.company_id],
            str(contact.id),
        )
    )

    eligible: list[tuple[UUID, UUID, str]] = []
    companies_with_email: set[UUID] = set()
    for contact in contacts:
        email = _normalize_recipient_email(contact.business_email)
        if email is None:
            continue
        eligible.append((contact.company_id, contact.id, email))
        companies_with_email.add(contact.company_id)

    skipped_company_ids = [
        company_id
        for company_id in company_ids
        if company_id not in companies_with_email
    ]
    return eligible, skipped_company_ids


def preview_campaign(
    db: Session,
    campaign: EmailCampaign,
    company_ids: Sequence[UUID],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    validate_gmail_account_access(
        db,
        org_id=campaign.org_id,
        user_id=campaign.created_by,
        gmail_account_ids=_campaign_account_ids(campaign),
    )
    contacts, skipped_company_ids = get_eligible_contacts(
        db,
        org_id=campaign.org_id,
        company_ids=company_ids,
    )
    assignments = _round_robin_accounts(
        _campaign_account_ids(campaign),
        len(contacts),
    )
    slots = schedule_account_assignments(
        assignments,
        campaign,
        start_at=now,
    )
    return {
        "eligible_contact_count": len(contacts),
        "skipped_companies_without_email": len(skipped_company_ids),
        "skipped_company_ids": skipped_company_ids,
        "daily_capacity_per_day": (
            len(campaign.gmail_account_ids) * campaign.daily_limit_per_account
        ),
        "estimated_completion_at": slots[-1][1] if slots else None,
    }


def start_campaign(
    db: Session,
    campaign: EmailCampaign,
    company_ids: Sequence[UUID],
    *,
    now: datetime | None = None,
) -> None:
    if campaign.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only draft campaigns can be started.",
        )
    validate_gmail_account_access(
        db,
        org_id=campaign.org_id,
        user_id=campaign.created_by,
        gmail_account_ids=_campaign_account_ids(campaign),
    )
    contacts, _ = get_eligible_contacts(
        db,
        org_id=campaign.org_id,
        company_ids=company_ids,
    )
    assignments = _round_robin_accounts(
        _campaign_account_ids(campaign),
        len(contacts),
    )
    slots = schedule_account_assignments(
        assignments,
        campaign,
        start_at=now,
    )
    queued_at = _as_utc(now or utc_now())
    recipients = [
        EmailCampaignRecipient(
            org_id=campaign.org_id,
            campaign_id=campaign.id,
            company_id=company_id,
            contact_id=contact_id,
            gmail_account_id=account_id,
            to_email=to_email,
            scheduled_at=scheduled_at,
            status="pending",
        )
        for (company_id, contact_id, to_email), (account_id, scheduled_at) in zip(
            contacts,
            slots,
        )
    ]
    if recipients:
        db.add_all(recipients)
    campaign.status = "running" if recipients else "completed"
    campaign.updated_at = queued_at
    db.commit()


def pause_campaign(db: Session, campaign: EmailCampaign) -> None:
    if campaign.status != "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only running campaigns can be paused.",
        )
    campaign.status = "paused"
    campaign.updated_at = utc_now()
    db.commit()


def resume_campaign(
    db: Session,
    campaign: EmailCampaign,
    *,
    now: datetime | None = None,
) -> None:
    if campaign.status != "paused":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only paused campaigns can be resumed.",
        )
    validate_gmail_account_access(
        db,
        org_id=campaign.org_id,
        user_id=campaign.created_by,
        gmail_account_ids=_campaign_account_ids(campaign),
    )

    pending = list(
        db.scalars(
            select(EmailCampaignRecipient)
            .where(
                EmailCampaignRecipient.campaign_id == campaign.id,
                EmailCampaignRecipient.org_id == campaign.org_id,
                EmailCampaignRecipient.status == "pending",
            )
            .order_by(
                EmailCampaignRecipient.scheduled_at,
                EmailCampaignRecipient.id,
            )
        ).all()
    )
    processing_exists = db.scalar(
        select(EmailCampaignRecipient.id)
        .where(
            EmailCampaignRecipient.campaign_id == campaign.id,
            EmailCampaignRecipient.status == "processing",
        )
        .limit(1)
    )
    if not pending and processing_exists is None:
        campaign.status = "completed"
        campaign.updated_at = utc_now()
        db.commit()
        return

    current_time = _as_utc(now or utc_now())
    sent_today = _sent_counts_today(db, campaign, current_time)
    slots = schedule_account_assignments(
        [recipient.gmail_account_id for recipient in pending],
        campaign,
        start_at=current_time,
        daily_sent_by_account=sent_today,
    )
    for recipient, (_, scheduled_at) in zip(pending, slots):
        recipient.scheduled_at = scheduled_at
        recipient.updated_at = current_time

    campaign.status = "running"
    campaign.updated_at = current_time
    db.commit()


def delete_campaign(db: Session, campaign: EmailCampaign) -> None:
    if campaign.status not in {"draft", "paused"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only draft or paused campaigns can be deleted.",
        )
    processing_exists = db.scalar(
        select(EmailCampaignRecipient.id)
        .where(
            EmailCampaignRecipient.campaign_id == campaign.id,
            EmailCampaignRecipient.status == "processing",
        )
        .limit(1)
    )
    if processing_exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A campaign with a delivery in progress cannot be deleted.",
        )
    for recipient in db.scalars(
        select(EmailCampaignRecipient).where(
            EmailCampaignRecipient.campaign_id == campaign.id,
            EmailCampaignRecipient.status == "pending",
        )
    ).all():
        recipient.status = "cancelled"
        recipient.updated_at = utc_now()
    db.flush()
    db.execute(
        delete(EmailCampaignRecipient).where(
            EmailCampaignRecipient.campaign_id == campaign.id
        )
    )
    db.delete(campaign)
    db.commit()


def campaign_recipient_counts(
    db: Session,
    campaign_ids: Sequence[UUID],
) -> dict[UUID, dict[str, int]]:
    result = {
        campaign_id: {
            "pending": 0,
            "processing": 0,
            "sent": 0,
            "failed": 0,
            "cancelled": 0,
        }
        for campaign_id in campaign_ids
    }
    if not campaign_ids:
        return result
    rows = db.execute(
        select(
            EmailCampaignRecipient.campaign_id,
            EmailCampaignRecipient.status,
            func.count(),
        )
        .where(EmailCampaignRecipient.campaign_id.in_(campaign_ids))
        .group_by(
            EmailCampaignRecipient.campaign_id,
            EmailCampaignRecipient.status,
        )
    ).all()
    for campaign_id, recipient_status, count in rows:
        if recipient_status in result[campaign_id]:
            result[campaign_id][recipient_status] = count
    return result


def maybe_complete_campaign(
    db: Session,
    campaign_id: UUID,
    *,
    now: datetime | None = None,
) -> None:
    campaign = db.get(EmailCampaign, campaign_id)
    if campaign is None or campaign.status != "running":
        return
    db.flush()
    active_recipient_id = db.scalar(
        select(EmailCampaignRecipient.id)
        .where(
            EmailCampaignRecipient.campaign_id == campaign_id,
            EmailCampaignRecipient.status.in_(("pending", "processing")),
        )
        .limit(1)
    )
    if active_recipient_id is None:
        campaign.status = "completed"
        campaign.updated_at = _as_utc(now or utc_now())


def schedule_account_assignments(
    account_assignments: Sequence[UUID],
    campaign: EmailCampaign,
    *,
    start_at: datetime | None = None,
    daily_sent_by_account: dict[UUID, int] | None = None,
) -> list[tuple[UUID, datetime]]:
    if not account_assignments:
        return []

    campaign_timezone = ZoneInfo(campaign.timezone)
    now_local = _as_utc(start_at or utc_now()).astimezone(campaign_timezone)
    day_counts: dict[UUID, int] = {
        account_id: (daily_sent_by_account or {}).get(account_id, 0)
        for account_id in set(account_assignments)
    }
    count_dates: dict[UUID, date] = {
        account_id: now_local.date() for account_id in set(account_assignments)
    }
    last_scheduled: dict[UUID, datetime] = {}
    scheduled: list[tuple[UUID, datetime]] = []

    for account_id in account_assignments:
        previous = last_scheduled.get(account_id)
        candidate = (
            now_local
            if previous is None
            else previous
            + timedelta(
                seconds=randint(
                    campaign.min_delay_seconds,
                    campaign.max_delay_seconds,
                )
            )
        )
        while True:
            if candidate.weekday() >= 5:
                candidate = _business_day_start(
                    candidate.date(),
                    campaign.send_window_start_hour,
                    campaign_timezone,
                )
            window_start = datetime.combine(
                candidate.date(),
                time(hour=campaign.send_window_start_hour),
                tzinfo=campaign_timezone,
            )
            window_end = datetime.combine(
                candidate.date(),
                time(hour=campaign.send_window_end_hour),
                tzinfo=campaign_timezone,
            )
            if candidate < window_start:
                candidate = window_start
            elif candidate >= window_end:
                candidate = _business_day_start(
                    candidate.date() + timedelta(days=1),
                    campaign.send_window_start_hour,
                    campaign_timezone,
                )
                continue

            if count_dates[account_id] != candidate.date():
                count_dates[account_id] = candidate.date()
                day_counts[account_id] = 0
            if day_counts[account_id] >= campaign.daily_limit_per_account:
                candidate = _business_day_start(
                    candidate.date() + timedelta(days=1),
                    campaign.send_window_start_hour,
                    campaign_timezone,
                )
                continue
            break

        day_counts[account_id] += 1
        last_scheduled[account_id] = candidate
        scheduled.append((account_id, candidate.astimezone(timezone.utc)))
    return scheduled


def _round_robin_accounts(
    account_ids: Sequence[UUID],
    recipient_count: int,
) -> list[UUID]:
    if recipient_count == 0:
        return []
    if not account_ids:
        raise ValueError("At least one Gmail account is required.")
    return [
        account_ids[index % len(account_ids)]
        for index in range(recipient_count)
    ]


def _campaign_account_ids(campaign: EmailCampaign) -> list[UUID]:
    return [UUID(account_id) for account_id in campaign.gmail_account_ids]


def _normalize_recipient_email(value: str | None) -> str | None:
    if not value:
        return None
    email = value.strip()
    if not email or _EMAIL_WHITESPACE.search(email):
        return None
    try:
        address = Address(addr_spec=email)
    except ValueError:
        return None
    if not address.username or not address.domain:
        return None
    return email


def _business_day_start(
    day: date,
    start_hour: int,
    campaign_timezone: ZoneInfo,
) -> datetime:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(
        day,
        time(hour=start_hour),
        tzinfo=campaign_timezone,
    )


def _sent_counts_today(
    db: Session,
    campaign: EmailCampaign,
    now: datetime,
) -> dict[UUID, int]:
    campaign_timezone = ZoneInfo(campaign.timezone)
    local_now = _as_utc(now).astimezone(campaign_timezone)
    local_start = datetime.combine(
        local_now.date(),
        time.min,
        tzinfo=campaign_timezone,
    )
    local_end = local_start + timedelta(days=1)
    rows = db.execute(
        select(
            EmailCampaignRecipient.gmail_account_id,
            func.count(),
        )
        .where(
            EmailCampaignRecipient.campaign_id == campaign.id,
            EmailCampaignRecipient.status == "sent",
            EmailCampaignRecipient.sent_at >= local_start.astimezone(timezone.utc),
            EmailCampaignRecipient.sent_at < local_end.astimezone(timezone.utc),
        )
        .group_by(EmailCampaignRecipient.gmail_account_id)
    ).all()
    return {account_id: count for account_id, count in rows}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
