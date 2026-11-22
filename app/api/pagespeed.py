from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.job_queue import (
    BatchJobRetryRequest,
    BatchJobRetryResponse,
    JobQueueStatusSummary,
    QueueJobsPageResponse,
)
from app.schemas.pagespeed import PageSpeedScanJobResponse
from app.services.pagespeed_jobs import (
    list_pagespeed_scan_jobs,
    retry_failed_pagespeed_scan_jobs,
    retry_pagespeed_scan_job,
    summarize_pagespeed_scan_jobs,
)


router = APIRouter(
    prefix="/pagespeed",
    tags=["PageSpeed"],
)


@router.get("/jobs/summary", response_model=JobQueueStatusSummary)
def pagespeed_job_summary(
    db: Annotated[Session, Depends(get_db)],
) -> JobQueueStatusSummary:
    return summarize_pagespeed_scan_jobs(db)


@router.get(
    "/jobs",
    response_model=QueueJobsPageResponse[PageSpeedScanJobResponse],
)
def list_pagespeed_jobs(
    db: Annotated[Session, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    search: Annotated[str, Query(max_length=300)] = "",
    status_filter: Literal[
        "queued",
        "running",
        "succeeded",
        "failed",
    ]
    | None = Query(default=None, alias="status"),
) -> QueueJobsPageResponse[PageSpeedScanJobResponse]:
    return list_pagespeed_scan_jobs(
        db,
        page=page,
        page_size=page_size,
        search=search,
        status=status_filter,
    )


@router.post(
    "/jobs/retry-batch",
    response_model=BatchJobRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_pagespeed_jobs_batch(
    payload: BatchJobRetryRequest,
    db: Annotated[Session, Depends(get_db)],
) -> BatchJobRetryResponse:
    return await retry_failed_pagespeed_scan_jobs(payload.job_ids, db)


@router.post(
    "/jobs/{job_id}/retry",
    response_model=PageSpeedScanJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_pagespeed_job(
    job_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> PageSpeedScanJobResponse:
    return await retry_pagespeed_scan_job(job_id, db)
