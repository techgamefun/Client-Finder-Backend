from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.website_crawl import (
    WebsiteCrawlBatchJobResponse,
    WebsiteCrawlBatchRequest,
    WebsiteCrawlBatchResponse,
    WebsiteCrawlJobResponse,
    WebsiteCrawlRequest,
)
from app.schemas.job_queue import (
    BatchJobRetryRequest,
    BatchJobRetryResponse,
)
from app.services.crawl_jobs import (
    create_website_crawl_job,
    create_website_crawl_jobs,
    get_website_crawl_job,
    list_website_crawl_jobs,
    retry_failed_website_crawl_jobs,
    retry_website_crawl_job,
)


router = APIRouter(
    prefix="/crawler",
    tags=["Website Crawler"],
)


@router.post(
    "/crawl/batch",
    response_model=WebsiteCrawlBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def crawl_websites(
    payload: WebsiteCrawlBatchRequest,
    db: Annotated[Session, Depends(get_db)],
) -> WebsiteCrawlBatchResponse:
    jobs = await create_website_crawl_jobs(
        [str(website_url) for website_url in payload.website_urls],
        db,
    )
    return WebsiteCrawlBatchResponse(
        jobs=[
            WebsiteCrawlBatchJobResponse(
                id=job.id,
                website_url=job.website_url,
            )
            for job in jobs
        ]
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
    "/jobs/retry-batch",
    response_model=BatchJobRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_crawl_jobs_batch(
    payload: BatchJobRetryRequest,
    db: Annotated[Session, Depends(get_db)],
) -> BatchJobRetryResponse:
    return await retry_failed_website_crawl_jobs(payload.job_ids, db)


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
