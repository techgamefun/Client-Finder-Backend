from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import CrawlerDomainFailure, ExcludedDomain
from app.schemas.excluded_domain import (
    ExcludedDomainBatchCreate,
    ExcludedDomainBatchResponse,
    ExcludedDomainCountResponse,
    ExcludedDomainCreate,
    ExcludedDomainReason,
    ExcludedDomainResponse,
)
from app.services.excluded_domains import (
    delete_queued_and_failed_jobs_for_domains,
)


router = APIRouter(prefix="/excluded-domains", tags=["Excluded Domains"])
DatabaseSession = Annotated[Session, Depends(get_db)]


@router.get("/count", response_model=ExcludedDomainCountResponse)
def count_excluded_domains(
    db: DatabaseSession,
    search: Annotated[str, Query(max_length=255)] = "",
    reason: ExcludedDomainReason | None = None,
) -> ExcludedDomainCountResponse:
    statement = select(func.count(ExcludedDomain.id))
    if search:
        statement = statement.where(
            ExcludedDomain.domain.contains(search, autoescape=True)
            | ExcludedDomain.note.contains(search, autoescape=True)
        )
    if reason is not None:
        statement = statement.where(ExcludedDomain.reason == reason)
    return ExcludedDomainCountResponse(total_count=db.scalar(statement) or 0)


@router.get("", response_model=list[ExcludedDomainResponse])
def list_excluded_domains(
    db: DatabaseSession,
    search: Annotated[str, Query(max_length=255)] = "",
    reason: ExcludedDomainReason | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
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
        delete_queued_and_failed_jobs_for_domains(db, {payload.domain})
        db.commit()
        db.refresh(domain)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This domain could not be added because it already exists.",
        ) from exc

    return ExcludedDomainResponse.model_validate(domain)


@router.post(
    "/batch",
    response_model=ExcludedDomainBatchResponse,
)
def create_excluded_domains_batch(
    payload: ExcludedDomainBatchCreate,
    db: DatabaseSession,
) -> ExcludedDomainBatchResponse:
    domains_by_name: dict[str, ExcludedDomainCreate] = {}
    for domain in payload.domains:
        domains_by_name.setdefault(domain.domain, domain)

    existing_domains = db.scalars(
        select(ExcludedDomain).where(
            ExcludedDomain.domain.in_(domains_by_name)
        )
    ).all()
    existing_by_name = {
        domain.domain: domain for domain in existing_domains
    }

    for domain_name, payload_domain in domains_by_name.items():
        existing = existing_by_name.get(domain_name)
        if existing is None:
            db.add(
                ExcludedDomain(
                    domain=domain_name,
                    reason=payload_domain.reason,
                    note=payload_domain.note,
                    source="user",
                )
            )
        else:
            existing.reason = payload_domain.reason
            existing.note = payload_domain.note
            existing.source = "user"

    try:
        db.flush()
        delete_queued_and_failed_jobs_for_domains(
            db,
            set(domains_by_name),
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Some domains could not be added because they changed concurrently.",
        ) from exc

    return ExcludedDomainBatchResponse(
        requested_count=len(payload.domains),
        created_count=sum(
            domain_name not in existing_by_name
            for domain_name in domains_by_name
        ),
        updated_count=len(existing_by_name),
        duplicate_count=len(payload.domains) - len(domains_by_name),
    )


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
