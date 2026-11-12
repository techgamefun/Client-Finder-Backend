from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.pagespeed import PageSpeedScanJobResponse
from app.services.pagespeed_jobs import (
    list_pagespeed_scan_jobs,
    retry_pagespeed_scan_job,
)


router = APIRouter(
    prefix="/pagespeed",
    tags=["PageSpeed"],
)


@router.get("/jobs", response_model=list[PageSpeedScanJobResponse])
def list_pagespeed_jobs(
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[PageSpeedScanJobResponse]:
    return list_pagespeed_scan_jobs(db, limit)


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
