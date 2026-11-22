from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_db
from app.schemas.website_crawl import (
    WebsiteCrawlBatchExcludedResponse,
    WebsiteCrawlBatchJobResponse,
    WebsiteCrawlBatchRequest,
    WebsiteCrawlBatchResponse,
    WebsiteCrawlJobResponse,
    WebsiteCrawlRequest,
)
from app.schemas.job_queue import (
    BatchJobRetryRequest,
    BatchJobRetryResponse,
    JobQueueStatusSummary,
    QueueJobsPageResponse,
)
from app.services.crawl_jobs import (
    create_website_crawl_job,
    create_website_crawl_jobs,
    get_website_crawl_job,
    list_website_crawl_jobs,
    retry_failed_website_crawl_jobs,
    retry_website_crawl_job,
    summarize_website_crawl_jobs,
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
    jobs, excluded = await create_website_crawl_jobs(
        [str(website_url) for website_url in payload.website_urls],
        db,
    )
    return WebsiteCrawlBatchResponse(
        jobs=[
            WebsiteCrawlBatchJobResponse(
                id=job.id,
                website_url=job.website_url,
                status=job.status,
            )
            for job in jobs
        ],
        excluded=[
            WebsiteCrawlBatchExcludedResponse(
                website_url=item.website_url,
                reason=item.reason,
            )
            for item in excluded
        ],
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
    return await run_in_threadpool(get_website_crawl_job, job.id, db)


@router.get(
    "/jobs",
    response_model=QueueJobsPageResponse[WebsiteCrawlJobResponse],
)
def list_crawl_jobs(
    db: Annotated[Session, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    search: Annotated[str, Query(max_length=300)] = "",
    status_filter: Literal["queued", "running", "succeeded", "failed"] | None = (
        Query(default=None, alias="status")
    ),
) -> QueueJobsPageResponse[WebsiteCrawlJobResponse]:
    return list_website_crawl_jobs(
        db,
        page=page,
        page_size=page_size,
        search=search,
        status=status_filter,
    )


@router.get("/jobs/summary", response_model=JobQueueStatusSummary)
def website_crawl_job_summary(
    db: Annotated[Session, Depends(get_db)],
) -> JobQueueStatusSummary:
    return summarize_website_crawl_jobs(db)


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
