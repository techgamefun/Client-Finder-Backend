from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.website_crawl import WebsiteCrawlRequest, WebsiteCrawlResponse
from app.services.website_crawler import crawl_and_persist


router = APIRouter(
    prefix="/crawler",
    tags=["Website Crawler"],
)


@router.post("/crawl", response_model=WebsiteCrawlResponse)
async def crawl_website(
    payload: WebsiteCrawlRequest,
    db: Annotated[Session, Depends(get_db)],
) -> WebsiteCrawlResponse:
    crawl = await crawl_and_persist(str(payload.website_url), db)
    return WebsiteCrawlResponse.model_validate(crawl)
