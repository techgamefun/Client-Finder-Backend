from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db,
    require_admin_context,
    require_authenticated_context,
)
from app.models import EmailCampaign, EmailTemplate
from app.schemas.email_templates import (
    EmailTemplateCreate,
    EmailTemplateDraftRequest,
    EmailTemplateDraftResponse,
    GroqEmailTemplateDraftResponse,
    EmailTemplatePreviewRequest,
    EmailTemplatePreviewResponse,
    EmailTemplateResponse,
    EmailTemplateSendRequest,
    EmailTemplateSendResponse,
    EmailTemplateUpdate,
    EmailTemplateVariableMetadata,
)
from app.services.email_templates import (
    TEMPLATE_VARIABLES,
    build_lead_context,
    build_sender_info,
    render_email_template,
    validate_email_template_syntax,
)
from app.services.application_settings import get_user_provider_api_key
from app.services.gemini_template_generator import generate_email_template_draft
from app.services.groq_template_generator import (
    generate_email_template_draft as generate_groq_template_draft,
)
from app.services.gmail import (
    get_gmail_account_email,
    send_gmail_message,
)
from app.tenant_context import TenantContext


router = APIRouter(
    prefix="/orgs/{org_id}/templates",
    tags=["Email templates"],
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


@router.get(
    "/variables",
    response_model=list[EmailTemplateVariableMetadata],
)
def list_template_variables(
    org_id: UUID,
    context: CurrentContext,
) -> list[EmailTemplateVariableMetadata]:
    _require_matching_organization(org_id, context)
    return [
        EmailTemplateVariableMetadata(
            **variable,
            placeholder=f"{{{{ {variable['path']} }}}}",
        )
        for variable in TEMPLATE_VARIABLES
    ]


@router.post(
    "",
    response_model=EmailTemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_email_template(
    org_id: UUID,
    payload: EmailTemplateCreate,
    context: AdminContext,
    db: DatabaseSession,
) -> EmailTemplate:
    _require_matching_organization(org_id, context)
    validate_email_template_syntax(payload.subject, payload.body_content)
    template = EmailTemplate(
        org_id=org_id,
        name=payload.name,
        subject=payload.subject,
        body_content=payload.body_content,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@router.post(
    "/generate-draft",
    response_model=GroqEmailTemplateDraftResponse,
    response_model_exclude_none=True,
)
async def generate_template_draft(
    org_id: UUID,
    payload: EmailTemplateDraftRequest,
    context: AdminContext,
    db: DatabaseSession,
) -> EmailTemplateDraftResponse | GroqEmailTemplateDraftResponse:
    _require_matching_organization(org_id, context)
    provider = payload.provider
    api_key = get_user_provider_api_key(
        db,
        provider,
        user_id=context.user_id,
    )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Add your {provider.capitalize()} API key in Settings "
                "before generating a draft."
            ),
        )
    if provider == "groq":
        return await generate_groq_template_draft(
            payload.prompt,
            api_key,
            model=payload.model,
        )
    return await generate_email_template_draft(payload.prompt, api_key)


@router.get("", response_model=list[EmailTemplateResponse])
def list_email_templates(
    org_id: UUID,
    context: CurrentContext,
    db: DatabaseSession,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[EmailTemplate]:
    _require_matching_organization(org_id, context)
    return list(
        db.scalars(
            select(EmailTemplate)
            .where(EmailTemplate.org_id == org_id)
            .order_by(EmailTemplate.created_at.desc(), EmailTemplate.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )


@router.get("/{template_id}", response_model=EmailTemplateResponse)
def get_email_template(
    org_id: UUID,
    template_id: UUID,
    context: CurrentContext,
    db: DatabaseSession,
) -> EmailTemplate:
    _require_matching_organization(org_id, context)
    return _get_email_template_or_404(db, org_id, template_id)


@router.patch("/{template_id}", response_model=EmailTemplateResponse)
def update_email_template(
    org_id: UUID,
    template_id: UUID,
    payload: EmailTemplateUpdate,
    context: AdminContext,
    db: DatabaseSession,
) -> EmailTemplate:
    _require_matching_organization(org_id, context)
    template = _get_email_template_or_404(db, org_id, template_id)
    changes = payload.model_dump(exclude_unset=True)
    subject = changes.get("subject", template.subject)
    body_content = changes.get("body_content", template.body_content)
    validate_email_template_syntax(subject, body_content)
    for field, value in changes.items():
        setattr(template, field, value)
    db.commit()
    db.refresh(template)
    return template


@router.delete(
    "/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_email_template(
    org_id: UUID,
    template_id: UUID,
    context: AdminContext,
    db: DatabaseSession,
) -> Response:
    _require_matching_organization(org_id, context)
    template = _get_email_template_or_404(db, org_id, template_id)
    campaign_reference = db.scalar(
        select(EmailCampaign.id)
        .where(
            EmailCampaign.org_id == org_id,
            EmailCampaign.template_id == template_id,
        )
        .limit(1)
    )
    if campaign_reference is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This template is used by an email campaign and cannot be deleted.",
        )
    db.delete(template)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This template is used by an email campaign and cannot be deleted.",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{template_id}/preview",
    response_model=EmailTemplatePreviewResponse,
)
def preview_email_template(
    org_id: UUID,
    template_id: UUID,
    payload: EmailTemplatePreviewRequest,
    context: CurrentContext,
    db: DatabaseSession,
) -> EmailTemplatePreviewResponse:
    _require_matching_organization(org_id, context)
    template = _get_email_template_or_404(db, org_id, template_id)
    sender_email = (
        get_gmail_account_email(db, payload.gmail_account_id)
        if payload.gmail_account_id is not None
        else context.email
    )
    lead_context = build_lead_context(
        db,
        payload.company_id,
        payload.contact_id,
        build_sender_info(
            db,
            organization_id=context.organization_id,
            user_email=context.email,
            role=context.role,
            email=sender_email,
        ),
    )
    subject, body_content = render_email_template(
        template.subject,
        template.body_content,
        lead_context,
        payload.custom_variables,
    )
    return EmailTemplatePreviewResponse(
        subject=subject,
        body_content=body_content,
    )


@router.post(
    "/{template_id}/send",
    response_model=EmailTemplateSendResponse,
)
def send_email_template(
    org_id: UUID,
    template_id: UUID,
    payload: EmailTemplateSendRequest,
    context: CurrentContext,
    db: DatabaseSession,
) -> EmailTemplateSendResponse:
    _require_matching_organization(org_id, context)
    template = _get_email_template_or_404(db, org_id, template_id)
    sender_email = get_gmail_account_email(db, payload.gmail_account_id)
    lead_context = build_lead_context(
        db,
        payload.company_id,
        payload.contact_id,
        build_sender_info(
            db,
            organization_id=context.organization_id,
            user_email=context.email,
            role=context.role,
            email=sender_email,
        ),
    )
    recipient_email = lead_context["contact"]["email"]
    if not recipient_email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The selected contact does not have a business email address.",
        )
    subject, body_content = render_email_template(
        template.subject,
        template.body_content,
        lead_context,
        payload.custom_variables,
    )
    sent = send_gmail_message(
        db,
        payload.gmail_account_id,
        recipient_email=recipient_email,
        subject=subject,
        body_content=body_content,
    )
    return EmailTemplateSendResponse(
        status="sent",
        message_id=sent.message_id,
        thread_id=sent.thread_id,
        recipient_email=recipient_email,
    )


def _require_matching_organization(
    org_id: UUID,
    context: TenantContext,
) -> None:
    if context.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found.",
        )


def _get_email_template_or_404(
    db: Session,
    org_id: UUID,
    template_id: UUID,
) -> EmailTemplate:
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
    return template


