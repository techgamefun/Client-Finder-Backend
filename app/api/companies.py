
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Company
from app.schemas.company import CompanyCreate, CompanyResponse
from app.services.normalization import (
    normalize_company_name,
    normalize_domain,
)


router = APIRouter(
    prefix="/companies",
    tags=["Companies"],
)

DatabaseSession = Annotated[Session, Depends(get_db)]


@router.post(
    "",
    response_model=CompanyResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_company(
    payload: CompanyCreate,
    db: DatabaseSession,
) -> Company:
    company_data = payload.model_dump(mode="json")

    company_data["normalized_name"] = normalize_company_name(
        company_data["name"]
    )

    company_data["domain"] = normalize_domain(
        company_data.get("domain")
    )

    if not company_data["domain"] and company_data.get("website"):
        company_data["domain"] = normalize_domain(
            str(company_data["website"])
        )

    company = Company(**company_data)
    db.add(company)

    try:
        db.commit()
        db.refresh(company)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A company with this domain already exists.",
        ) from exc

    return company


@router.get("", response_model=list[CompanyResponse])
def list_companies(
    db: DatabaseSession,
    limit: int = 50,
    offset: int = 0,
) -> list[Company]:
    if not 1 <= limit <= 100:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be between 1 and 100.",
        )

    if offset < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="offset must be non-negative.",
        )

    statement = (
        select(Company)
        .order_by(Company.id.desc())
        .limit(limit)
        .offset(offset)
    )

    return list(db.scalars(statement).all())
