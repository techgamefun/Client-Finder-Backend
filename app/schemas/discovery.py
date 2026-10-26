
from pydantic import BaseModel, Field, HttpUrl


class DiscoverySearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=300)
    max_results: int = Field(default=5, ge=1, le=10)


class DiscoveryResult(BaseModel):
    title: str
    url: HttpUrl
    content: str
    score: float


class DiscoverySearchResponse(BaseModel):
    query: str
    results: list[DiscoveryResult]
