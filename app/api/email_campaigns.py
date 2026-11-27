from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db,
    require_admin_context,
    require_authenticated_context,
)
from app.models import EmailCampaign
from app.schemas.email_campaigns import (
    EmailCampaignCreate,
    EmailCampaignPreviewRequest,
    EmailCampaignPreviewResponse,
    EmailCampaignRecipientCounts,
    EmailCampaignResponse,
)
from app.services.campaign_service import (
    campaign_recipient_counts,
    delete_campaign,
    get_campaign_or_404,
    pause_campaign,
    preview_campaign,
    resume_campaign,
    start_campaign,
    validate_campaign_resources,
)
from app.tenant_context import TenantContext


router = APIRouter(
    prefix="/orgs/{org_id}/campaigns",
    tags=["Email campaigns"],
)
DatabaseSession = Annotated[Session, Depends(get_db)]
AdminContext = Annotated[
    TenantContext,
    Depends(require_admin_context),
]
CurrentContext = Annotated[
    TenantContext,
    Depends(require_authenticated_context),
]


@router.post(
    "",
    response_model=EmailCampaignResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_email_campaign(
    org_id: UUID,
    payload: EmailCampaignCreate,
    context: AdminContext,
    db: DatabaseSession,
) -> EmailCampaignResponse:
    _require_matching_organization(org_id, context)
    if context.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )
    validate_campaign_resources(
        db,
        org_id=org_id,
        user_id=context.user_id,
        template_id=payload.template_id,
        follow_up_template_ids=[
            step.template_id for step in payload.follow_up_steps
        ],
        gmail_account_ids=payload.gmail_account_ids,
    )
    campaign = EmailCampaign(
        org_id=org_id,
        created_by=context.user_id,
        name=payload.name,
        template_id=payload.template_id,
        follow_up_steps=[
            {
                "template_id": str(step.template_id),
                "delay_days": step.delay_days,
            }
            for step in payload.follow_up_steps
        ],
        verify_mx=payload.verify_mx,
        gmail_account_ids=[str(account_id) for account_id in payload.gmail_account_ids],
        daily_limit_per_account=payload.daily_limit_per_account,
        min_delay_seconds=payload.min_delay_seconds,
        max_delay_seconds=payload.max_delay_seconds,
        send_window_start_hour=payload.send_window_start_hour,
        send_window_end_hour=payload.send_window_end_hour,
        timezone=payload.timezone,
        status="draft",
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return _campaign_response(db, campaign)


@router.get("", response_model=list[EmailCampaignResponse])
def list_email_campaigns(
    org_id: UUID,
    context: CurrentContext,
    db: DatabaseSession,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[EmailCampaignResponse]:
    _require_matching_organization(org_id, context)
    campaigns = list(
        db.scalars(
            select(EmailCampaign)
            .where(EmailCampaign.org_id == org_id)
            .order_by(EmailCampaign.created_at.desc(), EmailCampaign.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    counts = campaign_recipient_counts(
        db,
        [campaign.id for campaign in campaigns],
    )
    return [
        _campaign_response(
            db,
            campaign,
            recipient_counts=counts[campaign.id],
        )
        for campaign in campaigns
    ]


@router.get("/{campaign_id}", response_model=EmailCampaignResponse)
def get_email_campaign(
    org_id: UUID,
    campaign_id: UUID,
    context: CurrentContext,
    db: DatabaseSession,
) -> EmailCampaignResponse:
    _require_matching_organization(org_id, context)
    campaign = get_campaign_or_404(
        db,
        org_id=org_id,
        campaign_id=campaign_id,
    )
    return _campaign_response(db, campaign)


@router.post(
    "/{campaign_id}/preview",
    response_model=EmailCampaignPreviewResponse,
)
def preview_email_campaign(
    org_id: UUID,
    campaign_id: UUID,
    payload: EmailCampaignPreviewRequest,
    context: CurrentContext,
    db: DatabaseSession,
) -> EmailCampaignPreviewResponse:
    _require_matching_organization(org_id, context)
    campaign = get_campaign_or_404(
        db,
        org_id=org_id,
        campaign_id=campaign_id,
    )
    return EmailCampaignPreviewResponse(
        **preview_campaign(db, campaign, payload.website_ids)
    )


@router.post("/{campaign_id}/start", response_model=EmailCampaignResponse)
def start_email_campaign(
    org_id: UUID,
    campaign_id: UUID,
    payload: EmailCampaignPreviewRequest,
    context: AdminContext,
    db: DatabaseSession,
) -> EmailCampaignResponse:
    _require_matching_organization(org_id, context)
    campaign = get_campaign_or_404(
        db,
        org_id=org_id,
        campaign_id=campaign_id,
        for_update=True,
    )
    start_campaign(db, campaign, payload.website_ids)
    db.refresh(campaign)
    return _campaign_response(db, campaign)


@router.post("/{campaign_id}/pause", response_model=EmailCampaignResponse)
def pause_email_campaign(
    org_id: UUID,
    campaign_id: UUID,
    context: AdminContext,
    db: DatabaseSession,
) -> EmailCampaignResponse:
    _require_matching_organization(org_id, context)
    campaign = get_campaign_or_404(
        db,
        org_id=org_id,
        campaign_id=campaign_id,
        for_update=True,
    )
    pause_campaign(db, campaign)
    db.refresh(campaign)
    return _campaign_response(db, campaign)


@router.post("/{campaign_id}/resume", response_model=EmailCampaignResponse)
def resume_email_campaign(
    org_id: UUID,
    campaign_id: UUID,
    context: AdminContext,
    db: DatabaseSession,
) -> EmailCampaignResponse:
    _require_matching_organization(org_id, context)
    campaign = get_campaign_or_404(
        db,
        org_id=org_id,
        campaign_id=campaign_id,
        for_update=True,
    )
    resume_campaign(db, campaign)
    db.refresh(campaign)
    return _campaign_response(db, campaign)


@router.delete(
    "/{campaign_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_email_campaign(
    org_id: UUID,
    campaign_id: UUID,
    context: AdminContext,
    db: DatabaseSession,
) -> Response:
    _require_matching_organization(org_id, context)
    campaign = get_campaign_or_404(
        db,
        org_id=org_id,
        campaign_id=campaign_id,
        for_update=True,
    )
    delete_campaign(db, campaign)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _require_matching_organization(
    org_id: UUID,
    context: TenantContext,
) -> None:
    if context.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found.",
        )


def _campaign_response(
    db: Session,
    campaign: EmailCampaign,
    *,
    recipient_counts: dict[str, int] | None = None,
) -> EmailCampaignResponse:
    counts = recipient_counts
    if counts is None:
        counts = campaign_recipient_counts(db, [campaign.id])[campaign.id]
    return EmailCampaignResponse(
        id=campaign.id,
        org_id=campaign.org_id,
        created_by=campaign.created_by,
        name=campaign.name,
        template_id=campaign.template_id,
        follow_up_steps=campaign.follow_up_steps,
        verify_mx=campaign.verify_mx,
        gmail_account_ids=campaign.gmail_account_ids,
        daily_limit_per_account=campaign.daily_limit_per_account,
        min_delay_seconds=campaign.min_delay_seconds,
        max_delay_seconds=campaign.max_delay_seconds,
        send_window_start_hour=campaign.send_window_start_hour,
        send_window_end_hour=campaign.send_window_end_hour,
        timezone=campaign.timezone,
        status=campaign.status,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
        recipient_counts=EmailCampaignRecipientCounts(**counts),
    )
