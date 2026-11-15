import json
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import Website
from app.schemas.website import WebsiteReclassificationBatchResponse
from app.schemas.website_crawl import CrawlPageClassification
from app.services.website_registry import page_type_from_crawl_category


CLASSIFICATION_BATCH_SIZE = 25
CLASSIFICATION_BATCH_MAX_BYTES = 8 * 1024 * 1024


class SavedClassificationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    site_type: str
    website_topics: list[str] = Field(default_factory=list)


class SavedClassificationBatchItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    website_id: UUID
    site_type: str
    website_topics: list[str] = Field(default_factory=list)


class SavedClassificationBatchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    websites: list[SavedClassificationBatchItem]


async def reclassify_saved_website(website_id: UUID, db: Session) -> Website:
    website = db.scalar(
        select(Website)
        .options(selectinload(Website.crawl))
        .where(Website.id == website_id)
    )
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found.",
        )
    if website.crawl is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This website has no saved crawl data to classify.",
        )

    payload = _saved_classification_payload(website)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The saved crawl has no homepage classification signals.",
        )

    response = await _post_classifier("classify-saved", payload)
    try:
        classification = SavedClassificationResponse.model_validate(response.json())
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The crawler service returned an invalid saved classification.",
        ) from exc

    website.page_type = (
        page_type_from_crawl_category(classification.site_type) or "other"
    )
    website.site_topic = classification.website_topics
    website.classification_manual = False
    db.commit()
    db.refresh(website)
    return website


async def reclassify_unclassified_websites(
    db: Session,
) -> WebsiteReclassificationBatchResponse:
    websites = list(
        db.scalars(
            select(Website)
            .options(selectinload(Website.crawl))
            .where(Website.crawl_id.is_not(None))
        ).all()
    )
    eligible = [
        website
        for website in websites
        if not website.page_type or not website.site_topic
    ]

    payloads: list[dict[str, object]] = []
    skipped_count = 0
    for website in eligible:
        payload = _saved_classification_payload(website)
        if payload is None:
            skipped_count += 1
            continue
        payload["website_id"] = str(website.id)
        payloads.append(payload)

    classifications: dict[UUID, SavedClassificationBatchItem] = {}
    for batch_payloads in _classification_payload_batches(payloads):
        response = await _post_classifier(
            "classify-saved-batch",
            {"sites": batch_payloads},
        )
        try:
            batch = SavedClassificationBatchResponse.model_validate(
                response.json()
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The crawler service returned an invalid saved classification batch.",
            ) from exc

        expected_ids = {
            UUID(payload["website_id"])
            for payload in batch_payloads
        }
        returned_ids = [item.website_id for item in batch.websites]
        if (
            len(returned_ids) != len(expected_ids)
            or set(returned_ids) != expected_ids
        ):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The crawler service returned incomplete saved classifications.",
            )
        classifications.update(
            (item.website_id, item) for item in batch.websites
        )

    for website in eligible:
        classification = classifications.get(website.id)
        if classification is None:
            continue
        website.page_type = (
            page_type_from_crawl_category(classification.site_type) or "other"
        )
        website.site_topic = classification.website_topics
        website.classification_manual = False
    if classifications:
        db.commit()

    return WebsiteReclassificationBatchResponse(
        eligible_count=len(eligible),
        recomputed_count=len(classifications),
        skipped_count=skipped_count,
    )


def _classification_payload_batches(
    payloads: list[dict[str, object]],
) -> list[list[dict[str, object]]]:
    batches: list[list[dict[str, object]]] = []
    batch: list[dict[str, object]] = []
    batch_size = len('{"sites":[]}')
    for payload in payloads:
        payload_size = len(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        next_size = batch_size + payload_size + (1 if batch else 0)
        if batch and (
            len(batch) >= CLASSIFICATION_BATCH_SIZE
            or next_size > CLASSIFICATION_BATCH_MAX_BYTES
        ):
            batches.append(batch)
            batch = []
            batch_size = len('{"sites":[]}')
            next_size = batch_size + payload_size
        batch.append(payload)
        batch_size = next_size
    if batch:
        batches.append(batch)
    return batches


async def _post_classifier(path: str, payload: dict[str, object]) -> httpx.Response:
    service_url = f"{settings.crawler_service_url.rstrip('/')}/{path}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(service_url, json=payload)
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="The crawler service timed out while classifying saved data.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The crawler service is unavailable.",
        ) from exc

    if response.is_error:
        try:
            error_data = response.json()
        except ValueError:
            error_data = None
        detail = (
            error_data.get("detail")
            if isinstance(error_data, dict)
            else None
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=detail or "The crawler service failed to classify saved data.",
        )
    return response


def _saved_classification_payload(
    website: Website,
) -> dict[str, object] | None:
    crawl = website.crawl
    if crawl is None:
        return None
    pages = [
        CrawlPageClassification.model_validate(page)
        for page in crawl.classification_signals or []
    ]
    page = _homepage_classification(
        pages,
        website.website_url,
        crawl.home_page_url,
    )
    if page is None or page.classification_signals is None:
        return None
    return {
        "website_url": page.url,
        "body_text": crawl.home_content or "",
        "signals": page.classification_signals.model_dump(mode="json"),
    }


def _homepage_classification(
    pages: list[CrawlPageClassification],
    website_url: str,
    home_page_url: str | None,
) -> CrawlPageClassification | None:
    homepage_urls = {
        _url_key(url)
        for url in (home_page_url, website_url)
        if url is not None
    }
    return next(
        (
            page
            for page in pages
            if _url_key(page.url) in homepage_urls
        ),
        pages[0] if pages else None,
    )


def _url_key(value: str) -> tuple[str, str, str]:
    parsed = urlsplit(value)
    return (
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/"),
    )
