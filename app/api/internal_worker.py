import hmac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status
from starlette.concurrency import run_in_threadpool

from app.config import settings


router = APIRouter(prefix="/internal/worker")

_JOB_PROCESSORS = {
    "crawl": "process_job",
    "pagespeed": "process_pagespeed_job",
    "keywords": "process_corpus_analysis_job",
    "ahrefs": "process_ahrefs_domain_rating_job",
    "hypestat": "process_hypestat_fetch_job",
}
_JOB_RECOVERY = {
    "crawl": "_find_recoverable_job",
    "pagespeed": "_find_recoverable_pagespeed_job",
    "keywords": "_find_recoverable_corpus_analysis_job",
    "ahrefs": "_find_recoverable_ahrefs_domain_rating_job",
    "hypestat": "_find_recoverable_hypestat_fetch_job",
}


def _authorize_worker(token: str | None) -> None:
    expected = settings.worker_shared_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Background worker authentication is not configured.",
        )
    if token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid background worker token.",
        )


@router.post("/jobs/{job_type}/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def process_job(
    job_type: str,
    job_id: UUID,
    x_worker_token: Annotated[str | None, Header()] = None,
) -> None:
    _authorize_worker(x_worker_token)
    processor_name = _JOB_PROCESSORS.get(job_type)
    if processor_name is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown background job type.",
        )

    from app import worker

    await getattr(worker, processor_name)(job_id)


@router.post("/recover/{job_type}")
async def recover_job(
    job_type: str,
    x_worker_token: Annotated[str | None, Header()] = None,
) -> dict[str, str | None]:
    _authorize_worker(x_worker_token)
    recovery_name = _JOB_RECOVERY.get(job_type)
    if recovery_name is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown background job type.",
        )

    from app import worker

    job_id = await run_in_threadpool(getattr(worker, recovery_name))
    return {"job_id": str(job_id) if job_id is not None else None}


@router.post("/scheduled/{task_name}")
async def process_scheduled_task(
    task_name: str,
    x_worker_token: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    _authorize_worker(x_worker_token)
    if task_name == "campaign":
        from app.services.campaign_worker import process_due_campaign_recipient

        processed = await process_due_campaign_recipient()
    elif task_name == "gmail-monitor":
        from app.services.campaign_monitor import process_due_gmail_monitoring

        processed = await process_due_gmail_monitoring()
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown scheduled task.",
        )
    return {"processed": processed}
