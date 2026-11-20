from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import CrawlerDomainFailure, ExcludedDomain
from app.schemas.excluded_domain import (
    ExcludedDomainCreate,
    ExcludedDomainReason,
    ExcludedDomainResponse,
)
from app.services.excluded_domains import (
    mark_queued_jobs_skipped_for_domain,
)


router = APIRouter(prefix="/excluded-domains", tags=["Excluded Domains"])
DatabaseSession = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[ExcludedDomainResponse])
def list_excluded_domains(
    db: DatabaseSession,
    search: Annotated[str, Query(max_length=255)] = "",
    reason: ExcludedDomainReason | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[ExcludedDomainResponse]:
    statement = select(ExcludedDomain).order_by(
        ExcludedDomain.domain.asc(),
    )
    if search:
        statement = statement.where(
            ExcludedDomain.domain.contains(search, autoescape=True)
            | ExcludedDomain.note.contains(search, autoescape=True)
        )
    if reason is not None:
        statement = statement.where(ExcludedDomain.reason == reason)

    domains = db.scalars(statement.limit(limit).offset(offset)).all()
    return [
        ExcludedDomainResponse.model_validate(domain)
        for domain in domains
    ]


@router.post(
    "",
    response_model=ExcludedDomainResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_excluded_domain(
    payload: ExcludedDomainCreate,
    db: DatabaseSession,
) -> ExcludedDomainResponse:
    domain = db.scalar(
        select(ExcludedDomain).where(
            ExcludedDomain.domain == payload.domain
        )
    )
    if domain is None:
        domain = ExcludedDomain(
            domain=payload.domain,
            reason=payload.reason,
            note=payload.note,
            source="user",
        )
        db.add(domain)
    else:
        domain.reason = payload.reason
        domain.note = payload.note
        domain.source = "user"

    try:
        db.flush()
        mark_queued_jobs_skipped_for_domain(
            db,
            payload.domain,
            payload.reason,
        )
        db.commit()
        db.refresh(domain)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This domain could not be added because it already exists.",
        ) from exc

    return ExcludedDomainResponse.model_validate(domain)


@router.delete("/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_excluded_domain(
    domain_id: UUID,
    db: DatabaseSession,
) -> Response:
    domain = db.scalar(
        select(ExcludedDomain).where(ExcludedDomain.id == domain_id)
    )
    if domain is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Excluded domain not found.",
        )
    db.delete(domain)
    db.execute(
        delete(CrawlerDomainFailure).where(
            CrawlerDomainFailure.organization_id == domain.organization_id,
            CrawlerDomainFailure.domain == domain.domain,
        )
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
