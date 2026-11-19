from uuid import UUID

from pydantic import BaseModel, Field


class BatchJobRetryRequest(BaseModel):
    job_ids: list[UUID] = Field(min_length=1, max_length=100)


class BatchJobRetryFailure(BaseModel):
    job_id: UUID
    message: str


class BatchJobRetryResponse(BaseModel):
    retried_job_ids: list[UUID]
    failures: list[BatchJobRetryFailure]
