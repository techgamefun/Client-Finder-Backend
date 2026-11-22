from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Website, WebsiteCorpusAnalysis, WebsiteCrawl
from app.schemas.website import (
    CrawlerCorpusWordsResponse,
    WebsiteCorpusAnalysisResponse,
    WebsiteCorpusWordStat,
    WebsiteCorpusWordsResponse,
)
from app.services.normalization import normalize_domain

_TOP_N = 50
_ANALYSIS_VERSION = 2
_CRAWLER_TIMEOUT_SECONDS = 30.0
_MAX_ANALYSIS_BATCH_SIZE = 500


def get_website_corpus_words(
    db: Session,
    website_ids: list[UUID] | None = None,
) -> WebsiteCorpusWordsResponse:
    if website_ids is not None and not website_ids:
        return WebsiteCorpusWordsResponse()

    statement = (
        select(
            Website.id,
            Website.domain,
            WebsiteCrawl.classification_signals,
            WebsiteCrawl.crawled_at,
        )
        .join(WebsiteCrawl, Website.crawl_id == WebsiteCrawl.id)
        .order_by(Website.domain)
    )
    if website_ids is not None:
        statement = statement.where(Website.id.in_(website_ids))
    rows = db.execute(statement).all()
    if not rows:
        return WebsiteCorpusWordsResponse()

    website_ids = [row.id for row in rows]
    cached = {
        analysis.website_id: analysis
        for analysis in db.scalars(
            select(WebsiteCorpusAnalysis).where(
                WebsiteCorpusAnalysis.website_id.in_(website_ids),
            )
        ).all()
    }
    stale_rows = [
        row
        for row in rows
        if not _is_current(cached.get(row.id), row.crawled_at)
    ]

    if stale_rows:
        words_by_domain = _analyze_sites(
            [
                _site_text_record(row.domain, row.classification_signals)
                for row in stale_rows
            ],
        )
        for row in stale_rows:
            domain = normalize_domain(row.domain)
            if domain is None or domain not in words_by_domain:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="The crawler service omitted a website corpus result.",
                )
            analysis = cached.get(row.id)
            if analysis is None:
                analysis = WebsiteCorpusAnalysis(website_id=row.id)
                db.add(analysis)
                cached[row.id] = analysis
            analysis.words = [
                word.model_dump() for word in words_by_domain[domain]
            ]
            analysis.algorithm_version = _ANALYSIS_VERSION
            analysis.source_crawled_at = row.crawled_at
            analysis.computed_at = datetime.now(timezone.utc)

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return _read_concurrent_results(db, rows)
        except Exception:
            db.rollback()
            raise

    return WebsiteCorpusWordsResponse(
        websites=[
            WebsiteCorpusAnalysisResponse(
                website_id=row.id,
                domain=row.domain,
                words=cached[row.id].words,
                computed_at=_as_utc(cached[row.id].computed_at),
            )
            for row in rows
            if row.id in cached
        ],
    )


def _is_current(
    analysis: WebsiteCorpusAnalysis | None,
    crawled_at: datetime,
) -> bool:
    return (
        analysis is not None
        and analysis.algorithm_version == _ANALYSIS_VERSION
        and _same_timestamp(analysis.source_crawled_at, crawled_at)
    )


def _same_timestamp(
    left: datetime | None,
    right: datetime | None,
) -> bool:
    if left is None or right is None:
        return left is right
    left_utc = (
        left.replace(tzinfo=timezone.utc)
        if left.tzinfo is None
        else left.astimezone(timezone.utc)
    )
    right_utc = (
        right.replace(tzinfo=timezone.utc)
        if right.tzinfo is None
        else right.astimezone(timezone.utc)
    )
    return left_utc == right_utc


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _read_concurrent_results(
    db: Session,
    rows: list[Row[tuple[UUID, str, list[dict[str, Any]], datetime]]],
) -> WebsiteCorpusWordsResponse:
    analyses = {
        analysis.website_id: analysis
        for analysis in db.scalars(
            select(WebsiteCorpusAnalysis).where(
                WebsiteCorpusAnalysis.website_id.in_(
                    [row.id for row in rows],
                ),
            )
        ).all()
    }
    if any(
        not _is_current(analyses.get(row.id), row.crawled_at)
        for row in rows
    ):
        raise RuntimeError(
            "Concurrent website corpus analysis could not be persisted.",
        )
    return WebsiteCorpusWordsResponse(
        websites=[
            WebsiteCorpusAnalysisResponse(
                website_id=row.id,
                domain=row.domain,
                words=analyses[row.id].words,
                computed_at=_as_utc(analyses[row.id].computed_at),
            )
            for row in rows
        ],
    )


def _site_text_record(
    domain: str,
    classification_signals: list[dict[str, object]],
) -> dict[str, object]:
    titles: list[str] = []
    descriptions: list[str] = []
    for page in classification_signals:
        signals = page.get("classification_signals")
        if not isinstance(signals, dict):
            continue
        title = signals.get("page_title")
        description = signals.get("meta_description")
        if isinstance(title, str) and title.strip():
            titles.append(title)
        if isinstance(description, str) and description.strip():
            descriptions.append(description)
    return {
        "domain": domain,
        "titles": titles,
        "descriptions": descriptions,
    }


def _analyze_sites(
    sites: list[dict[str, object]],
) -> dict[str, list[WebsiteCorpusWordStat]]:
    words_by_domain: dict[str, list[WebsiteCorpusWordStat]] = {}
    for start in range(0, len(sites), _MAX_ANALYSIS_BATCH_SIZE):
        batch = sites[start : start + _MAX_ANALYSIS_BATCH_SIZE]
        expected_domains = {
            normalize_domain(str(site["domain"]))
            for site in batch
        }
        try:
            response = httpx.post(
                f"{settings.crawler_service_url.rstrip('/')}/corpus-words",
                json={"sites": batch, "top_n": _TOP_N},
                timeout=_CRAWLER_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=(
                    "The crawler service timed out while analyzing website text."
                ),
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The crawler service is unavailable for corpus analysis.",
            ) from exc

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The crawler service rejected the corpus analysis request.",
            ) from exc

        try:
            result = CrawlerCorpusWordsResponse.model_validate_json(
                response.content,
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "The crawler service returned an incompatible corpus "
                    "response. Redeploy or restart crawler-service so "
                    "/corpus-words returns per-domain site results, then retry."
                ),
            ) from exc

        for site_result in result.sites:
            domain = normalize_domain(site_result.domain)
            if (
                domain is None
                or domain not in expected_domains
                or domain in words_by_domain
            ):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="The crawler service returned duplicate or invalid domains.",
                )
            words_by_domain[domain] = site_result.words
        returned_domains = {
            normalize_domain(site_result.domain)
            for site_result in result.sites
        }
        if returned_domains != expected_domains:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The crawler service omitted a website corpus result.",
            )

    return words_by_domain


