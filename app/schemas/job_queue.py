from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field


QueueItem = TypeVar("QueueItem")


class BatchJobRetryRequest(BaseModel):
    job_ids: list[UUID] = Field(min_length=1, max_length=100)


class BatchJobRetryFailure(BaseModel):
    job_id: UUID
    message: str


class BatchJobRetryResponse(BaseModel):
    retried_job_ids: list[UUID]
    failures: list[BatchJobRetryFailure]


class JobQueueStatusSummary(BaseModel):
    running_count: int
    failed_count: int


class QueueJobsPageResponse(BaseModel, Generic[QueueItem]):
    items: list[QueueItem]
    total_count: int
    page: int
    page_size: int
    status_counts: dict[str, int]
