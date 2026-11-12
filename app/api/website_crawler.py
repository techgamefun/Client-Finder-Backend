from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.website_crawl import (
    WebsiteCrawlJobResponse,
    WebsiteCrawlRequest,
)
from app.services.crawl_jobs import (
    create_website_crawl_job,
    get_website_crawl_job,
    list_website_crawl_jobs,
    retry_website_crawl_job,
)


router = APIRouter(
    prefix="/crawler",
    tags=["Website Crawler"],
)


@router.post(
    "/crawl",
    response_model=WebsiteCrawlJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def crawl_website(
    payload: WebsiteCrawlRequest,
    db: Annotated[Session, Depends(get_db)],
) -> WebsiteCrawlJobResponse:
    job = await create_website_crawl_job(str(payload.website_url), db)
    return get_website_crawl_job(job.id, db)


@router.get("/jobs", response_model=list[WebsiteCrawlJobResponse])
def list_crawl_jobs(
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[WebsiteCrawlJobResponse]:
    return list_website_crawl_jobs(db, limit)


@router.post(
    "/jobs/{job_id}/retry",
    response_model=WebsiteCrawlJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_crawl_job(
    job_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> WebsiteCrawlJobResponse:
    return await retry_website_crawl_job(job_id, db)


@router.get("/jobs/{job_id}", response_model=WebsiteCrawlJobResponse)
def get_crawl_job(
    job_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> WebsiteCrawlJobResponse:
    return get_website_crawl_job(job_id, db)
