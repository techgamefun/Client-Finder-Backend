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
from app.models import EmailTemplate, Organization
from app.schemas.email_templates import (
    EmailTemplateCreate,
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
    render_email_template,
    validate_email_template_syntax,
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
    db.delete(template)
    db.commit()
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
        _sender_info(db, context, email=sender_email),
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
        _sender_info(db, context, email=sender_email),
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


def _sender_info(
    db: Session,
    context: TenantContext,
    *,
    email: str | None = None,
) -> dict[str, str | None]:
    account_email = email or context.email
    local_part = account_email.partition("@")[0]
    sender_name = " ".join(
        part
        for part in local_part.replace(".", " ").replace("_", " ").split()
    ).title()
    organization_name = db.scalar(
        select(Organization.name).where(
            Organization.id == context.organization_id
        )
    )
    return {
        "name": sender_name or None,
        "email": account_email,
        "role": context.role,
        "organization": organization_name,
    }
