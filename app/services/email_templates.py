import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from jinja2 import StrictUndefined, TemplateError, Undefined
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Company, Contact, Organization, Website, WebsiteCrawl
from app.services.normalization import normalize_domain
from app.tenant_context import get_tenant_context


MAX_CRAWLED_TEXT_CHARACTERS = 50_000
MAX_RENDERED_BODY_CHARACTERS = 500_000
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

TEMPLATE_VARIABLES = (
    {
        "path": "company.id",
        "label": "Company ID",
        "description": "The UUID of the company profile.",
    },
    {
        "path": "company.name",
        "label": "Company name",
        "description": "The company profile name.",
    },
    {
        "path": "company.normalized_name",
        "label": "Normalized company name",
        "description": "The normalized company name used for matching.",
    },
    {
        "path": "company.domain",
        "label": "Company domain",
        "description": "The normalized domain from the company profile.",
    },
    {
        "path": "company.website",
        "label": "Company website",
        "description": "The website URL from the company profile.",
    },
    {
        "path": "company.industry",
        "label": "Industry",
        "description": "The company profile industry.",
    },
    {
        "path": "company.city",
        "label": "City",
        "description": "The company profile city.",
    },
    {
        "path": "company.state",
        "label": "State",
        "description": "The company profile state or region.",
    },
    {
        "path": "company.phone",
        "label": "Contact phone",
        "description": "The selected contact's business phone, if available.",
    },
    {
        "path": "company.status",
        "label": "Lead status",
        "description": "The lead status stored on the company profile.",
    },
    {
        "path": "company.lead_status",
        "label": "Lead status",
        "description": "The lead status stored on the company profile.",
    },
    {
        "path": "company.lead_score",
        "label": "Lead score",
        "description": "The lead score, if one has been set.",
    },
    {
        "path": "company.software_need",
        "label": "Software need",
        "description": "The software need recorded on the company profile.",
    },
    {
        "path": "company.crawled_text",
        "label": "Crawled website text",
        "description": "A text summary of available crawl pages for the company domain.",
    },
    {
        "path": "company.created_at",
        "label": "Company created date",
        "description": "When the company profile was added.",
    },
    {
        "path": "contact.id",
        "label": "Contact ID",
        "description": "The UUID of the selected contact.",
    },
    {
        "path": "contact.name",
        "label": "Contact name",
        "description": "The selected contact's full name.",
    },
    {
        "path": "contact.first_name",
        "label": "Contact first name",
        "description": "The first word of the selected contact's name.",
    },
    {
        "path": "contact.last_name",
        "label": "Contact last name",
        "description": "The remainder of the selected contact's name.",
    },
    {
        "path": "contact.role",
        "label": "Contact role",
        "description": "The selected contact's role.",
    },
    {
        "path": "contact.email",
        "label": "Contact email",
        "description": "The selected contact's business email.",
    },
    {
        "path": "contact.business_email",
        "label": "Contact business email",
        "description": "The selected contact's business email.",
    },
    {
        "path": "contact.phone",
        "label": "Contact phone",
        "description": "The selected contact's business phone.",
    },
    {
        "path": "contact.business_phone",
        "label": "Contact business phone",
        "description": "The selected contact's business phone.",
    },
    {
        "path": "contact.is_publicly_listed",
        "label": "Publicly listed contact",
        "description": "Whether the contact is marked as publicly listed.",
    },
    {
        "path": "sender.name",
        "label": "Sender name",
        "description": "Derived from the signed-in user's email unless overridden.",
    },
    {
        "path": "sender.email",
        "label": "Sender email",
        "description": "The selected connected Gmail address, or the signed-in user's email in a preview.",
    },
    {
        "path": "sender.role",
        "label": "Sender role",
        "description": "The signed-in user's organization role.",
    },
    {
        "path": "sender.organization",
        "label": "Organization name",
        "description": "The signed-in user's organization name.",
    },
)


def build_sender_info(
    db: Session,
    *,
    organization_id: UUID,
    user_email: str,
    role: str,
    email: str | None = None,
) -> dict[str, str | None]:
    account_email = email or user_email
    local_part = account_email.partition("@")[0]
    sender_name = " ".join(
        part
        for part in local_part.replace(".", " ").replace("_", " ").split()
    ).title()
    organization_name = db.scalar(
        select(Organization.name).where(Organization.id == organization_id)
    )
    return {
        "name": sender_name or None,
        "email": account_email,
        "role": role,
        "organization": organization_name,
    }


def build_lead_context(
    db: Session,
    company_id: UUID,
    contact_id: UUID | None,
    sender_info: Mapping[str, Any] | None,
) -> dict[str, Any]:
    tenant_context = get_tenant_context() or db.info.get("tenant_context")
    if tenant_context is None or tenant_context.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required to build an email template context.",
        )

    company = db.scalar(
        select(Company)
        .where(
            Company.id == company_id,
            Company.organization_id == tenant_context.organization_id,
        )
    )
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    contact = None
    if contact_id is not None:
        contact = db.scalar(
            select(Contact).where(
                Contact.id == contact_id,
                Contact.company_id == company.id,
                Contact.organization_id == tenant_context.organization_id,
            )
        )
        if contact is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Contact not found for this company.",
            )

    contact_name = contact.name if contact is not None else None
    first_name, last_name = _split_name(contact_name)
    crawled_text = _get_company_crawled_text(
        db,
        company,
        tenant_context.organization_id,
    )
    return {
        "company": {
            "id": str(company.id),
            "name": company.name,
            "normalized_name": company.normalized_name,
            "website": company.website,
            "domain": company.domain,
            "industry": company.industry,
            "city": company.city,
            "state": company.state,
            "phone": contact.business_phone if contact is not None else None,
            "status": company.lead_status,
            "lead_status": company.lead_status,
            "lead_score": company.lead_score,
            "software_need": company.software_need,
            "crawled_text": crawled_text,
            "created_at": company.created_at,
        },
        "contact": {
            "id": str(contact.id) if contact is not None else None,
            "name": contact_name,
            "first_name": first_name,
            "last_name": last_name,
            "role": contact.role if contact is not None else None,
            "email": contact.business_email if contact is not None else None,
            "business_email": (
                contact.business_email if contact is not None else None
            ),
            "phone": contact.business_phone if contact is not None else None,
            "business_phone": (
                contact.business_phone if contact is not None else None
            ),
            "is_publicly_listed": (
                contact.is_publicly_listed if contact is not None else False
            ),
        },
        "sender": dict(sender_info or {}),
    }


def validate_email_template_syntax(subject: str, body_content: str) -> None:
    try:
        _create_environment(autoescape=False).from_string(subject)
        _create_environment(autoescape=True).from_string(body_content)
    except TemplateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Email template contains invalid Jinja syntax: {exc}",
        ) from exc


def render_email_template(
    subject: str,
    body_content: str,
    context: Mapping[str, Any],
    custom_variables: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    render_context = _merge_custom_variables(context, custom_variables or {})
    try:
        rendered_subject = _create_environment(autoescape=False).from_string(
            subject
        ).render(render_context)
        rendered_body = _create_environment(autoescape=True).from_string(
            body_content
        ).render(render_context)
    except TemplateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Email template could not be rendered: {exc}",
        ) from exc

    if (
        not rendered_subject.strip()
        or "\r" in rendered_subject
        or "\n" in rendered_subject
        or len(rendered_subject) > 998
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The rendered subject must be non-empty, single-line, and no longer than 998 characters.",
        )
    if len(rendered_body) > MAX_RENDERED_BODY_CHARACTERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The rendered email body is too large.",
        )
    return rendered_subject.strip(), rendered_body


def _create_environment(*, autoescape: bool) -> SandboxedEnvironment:
    environment = SandboxedEnvironment(
        autoescape=autoescape,
        undefined=StrictUndefined,
    )
    environment.globals.clear()
    environment.filters.pop("safe", None)
    environment.filters["default"] = _default_filter
    environment.filters["d"] = _default_filter
    return environment


def _default_filter(
    value: Any,
    default_value: Any = "",
    boolean: bool = False,
) -> Any:
    if isinstance(value, Undefined) or value is None:
        return default_value
    if isinstance(value, str) and not value.strip():
        return default_value
    if boolean and not value:
        return default_value
    return value


def _merge_custom_variables(
    context: Mapping[str, Any],
    custom_variables: Mapping[str, Any],
) -> dict[str, Any]:
    merged = _copy_mapping(context)
    for key, value in custom_variables.items():
        if not isinstance(key, str) or not key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Custom variable names must be non-empty strings.",
            )
        path = key.split(".")
        if any(not _IDENTIFIER.fullmatch(part) for part in path):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid custom variable path: {key}",
            )
        if ".".join(path) == "sender.email":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="sender.email is determined by the connected account and cannot be overridden.",
            )
        if len(path) == 1:
            if key in {"company", "contact", "sender"}:
                if not isinstance(value, Mapping):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"The {key} custom variable must be an object.",
                    )
                _merge_mapping(merged[key], value, prefix=key)
            else:
                merged[key] = value
            continue

        target = merged
        for segment in path[:-1]:
            nested = target.get(segment)
            if nested is None:
                nested = {}
                target[segment] = nested
            if not isinstance(nested, dict):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Custom variable path is not an object: {key}",
                )
            target = nested
        target[path[-1]] = value
    return merged


def _copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, item in value.items():
        copied[key] = _copy_mapping(item) if isinstance(item, Mapping) else item
    return copied


def _merge_mapping(
    target: dict[str, Any],
    overrides: Mapping[str, Any],
    *,
    prefix: str,
) -> None:
    for key, value in overrides.items():
        if not isinstance(key, str) or not _IDENTIFIER.fullmatch(key):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Custom variable names must use letters, numbers, and underscores.",
            )
        path = f"{prefix}.{key}"
        if path == "sender.email":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="sender.email is determined by the connected account and cannot be overridden.",
            )
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge_mapping(target[key], value, prefix=path)
        else:
            target[key] = value


def _split_name(name: str | None) -> tuple[str | None, str | None]:
    if not name or not name.strip():
        return None, None
    parts = name.strip().split(maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else None


def _get_company_crawled_text(
    db: Session,
    company: Company,
    organization_id: UUID,
) -> str:
    domain = normalize_domain(company.domain or company.website)
    if not domain:
        return ""
    websites = db.scalars(
        select(Website)
        .options(selectinload(Website.crawl))
        .where(
            Website.organization_id == organization_id,
            Website.domain == domain,
        )
        .order_by(Website.updated_at.desc())
    ).all()
    sections: list[str] = []
    for website in websites:
        crawl = website.crawl
        if crawl is None:
            continue
        for label, content in (
            ("Home", crawl.home_content),
            ("About", crawl.about_content),
            ("Services", crawl.service_content),
            ("Contact", crawl.contact_content),
            ("Goals", crawl.goals_content),
            ("Blog", crawl.blog_content),
        ):
            if content and content.strip():
                sections.append(f"{label}:\n{content.strip()}")
    return "\n\n".join(sections)[:MAX_CRAWLED_TEXT_CHARACTERS]
