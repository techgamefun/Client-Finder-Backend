from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Website,
    WebsiteCorpusAnalysis,
    WebsiteCorpusAnalysisJob,
    WebsiteCrawl,
)
from app.schemas.website import (
    WebsiteCorpusAnalysisResponse,
    WebsiteCorpusWordsResponse,
)
from app.services.website_corpus_jobs import create_website_corpus_analysis_job

_ANALYSIS_VERSION = 2


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
    jobs = (
        db.scalars(
            select(WebsiteCorpusAnalysisJob).where(
                WebsiteCorpusAnalysisJob.website_id.in_(
                    [row.id for row in stale_rows],
                ),
            )
        ).all()
        if stale_rows
        else []
    )
    job_statuses = {
        (job.website_id, job.source_crawled_at): job.status
        for job in jobs
    }

    return WebsiteCorpusWordsResponse(
        websites=[
            WebsiteCorpusAnalysisResponse(
                website_id=row.id,
                domain=row.domain,
                words=cached[row.id].words,
                computed_at=_as_utc(cached[row.id].computed_at),
            )
            for row in rows
            if _is_current(cached.get(row.id), row.crawled_at)
        ],
        pending_website_ids=[
            row.id
            for row in stale_rows
            if job_statuses.get((row.id, row.crawled_at))
            in ("queued", "running")
        ],
        failed_website_ids=[
            row.id
            for row in stale_rows
            if job_statuses.get((row.id, row.crawled_at)) == "failed"
        ],
    )


def queue_stale_website_corpus_analysis(
    db: Session,
    website_ids: list[UUID] | None = None,
) -> list[UUID]:
    statement = (
        select(
            Website.id,
            WebsiteCrawl.crawled_at,
        )
        .join(WebsiteCrawl, Website.crawl_id == WebsiteCrawl.id)
        .order_by(Website.domain)
    )
    if website_ids is not None:
        if not website_ids:
            return []
        statement = statement.where(Website.id.in_(website_ids))
    rows = db.execute(statement).all()
    if not rows:
        return []

    website_ids = [row.id for row in rows]
    cached = {
        analysis.website_id: analysis
        for analysis in db.scalars(
            select(WebsiteCorpusAnalysis).where(
                WebsiteCorpusAnalysis.website_id.in_(website_ids),
            )
        ).all()
    }
    job_ids: list[UUID] = []
    for row in rows:
        if _is_current(cached.get(row.id), row.crawled_at):
            continue
        job = create_website_corpus_analysis_job(
            row.id,
            row.crawled_at,
            db,
        )
        if job is not None:
            job_ids.append(job.id)

    if job_ids:
        db.commit()
    return job_ids


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


