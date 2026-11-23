import asyncio
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from app import worker
from app.models import (
    Website,
    WebsiteAhrefsDomainRatingJob,
    WebsiteCorpusAnalysis,
    WebsiteCorpusAnalysisJob,
    WebsiteCrawl,
    WebsiteCrawlJob,
)
from app.schemas.website import WebsiteCorpusWordStat
from app.services import website_corpus_words
from app.services.website_crawler import CrawlData
from conftest import TestingSessionLocal


def _create_crawled_website(
    db,
    domain: str,
    *,
    crawled_at: datetime,
) -> Website:
    website_url = f"https://{domain}/"
    crawl = WebsiteCrawl(
        website_url=website_url,
        classification_signals=[
            {
                "url": website_url,
                "classification_signals": {
                    "page_title": f"{domain} services",
                    "meta_description": f"Services from {domain}",
                },
            },
        ],
        crawled_at=crawled_at,
    )
    db.add(crawl)
    db.flush()
    website = Website(
        website_url=website_url,
        domain=domain,
        crawl_id=crawl.id,
        discovery_source="manual",
    )
    db.add(website)
    db.flush()
    return website


def _disable_keyword_dispatch(monkeypatch):
    async def no_dispatch(_job_ids):
        return None

    monkeypatch.setattr(
        "app.api.websites.enqueue_website_corpus_analysis_jobs",
        no_dispatch,
    )


def test_corpus_words_returns_empty_without_crawled_websites(client):
    response = client.get("/api/v1/websites/corpus-words")

    assert response.status_code == 200
    assert response.json() == {
        "websites": [],
        "pending_website_ids": [],
        "failed_website_ids": [],
    }


def test_corpus_words_endpoint_queues_stale_sites_without_analyzing(
    client,
    monkeypatch,
):
    _disable_keyword_dispatch(monkeypatch)
    crawled_at = datetime(2026, 11, 14, tzinfo=timezone.utc)
    with TestingSessionLocal() as db:
        visible = _create_crawled_website(
            db,
            "visible.example",
            crawled_at=crawled_at,
        )
        _create_crawled_website(
            db,
            "off-page.example",
            crawled_at=crawled_at,
        )
        visible_id = visible.id
        db.commit()

    def should_not_analyze(*_args, **_kwargs):
        raise AssertionError("keyword generation must run in the worker")

    monkeypatch.setattr(website_corpus_words, "_analyze_sites", should_not_analyze)
    response = client.get(
        "/api/v1/websites/corpus-words",
        params={"website_ids": str(visible_id)},
    )

    assert response.status_code == 200
    assert response.json()["websites"] == []
    assert response.json()["pending_website_ids"] == [str(visible_id)]
    with TestingSessionLocal() as db:
        jobs = db.scalars(select(WebsiteCorpusAnalysisJob)).all()
        assert len(jobs) == 1
        assert jobs[0].website_id == visible_id

    queue_response = client.get("/api/v1/websites/corpus-words/jobs")
    assert queue_response.status_code == 200
    queue_result = queue_response.json()
    assert len(queue_result["items"]) == 1
    assert queue_result["items"][0]["website_id"] == str(visible_id)
    assert queue_result["items"][0]["status"] == "queued"
    assert queue_result["total_count"] == 1
    assert queue_result["status_counts"] == {"queued": 1}


def test_corpus_analysis_jobs_search_status_and_pagination(client):
    crawled_at = datetime(2026, 11, 14, tzinfo=timezone.utc)
    with TestingSessionLocal() as db:
        jobs = []
        for domain, status in (
            ("alpha-one.example", "queued"),
            ("alpha-two.example", "queued"),
            ("alpha-three.example", "failed"),
            ("beta.example", "queued"),
        ):
            website = _create_crawled_website(
                db,
                domain,
                crawled_at=crawled_at,
            )
            job = WebsiteCorpusAnalysisJob(
                website_id=website.id,
                source_crawled_at=crawled_at,
                status=status,
            )
            db.add(job)
            jobs.append(job)
        db.commit()
        expected_job_ids = {str(job.id) for job in jobs[:2]}

    response = client.get(
        "/api/v1/websites/corpus-words/jobs",
        params={
            "search": "alpha",
            "status": "queued",
            "page": 2,
            "page_size": 1,
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert len(result["items"]) == 1
    assert result["items"][0]["id"] in expected_job_ids
    assert result["total_count"] == 2
    assert result["page"] == 2
    assert result["page_size"] == 1
    assert result["status_counts"] == {"queued": 2, "failed": 1}


def test_corpus_worker_persists_keywords_and_endpoint_reads_cache(
    client,
    monkeypatch,
):
    _disable_keyword_dispatch(monkeypatch)
    crawled_at = datetime(2026, 11, 14, tzinfo=timezone.utc)
    with TestingSessionLocal() as db:
        website = _create_crawled_website(
            db,
            "example.com",
            crawled_at=crawled_at,
        )
        website_id = website.id
        job = WebsiteCorpusAnalysisJob(
            website_id=website.id,
            source_crawled_at=crawled_at,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    def analyze(sites):
        assert sites == [
            {
                "domain": "example.com",
                "titles": ["example.com services"],
                "descriptions": ["Services from example.com"],
            },
        ]
        return {
            "example.com": [
                WebsiteCorpusWordStat(
                    word="home services",
                    score=1.5,
                    is_phrase=True,
                ),
            ],
        }

    monkeypatch.setattr(worker, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(worker, "_analyze_sites", analyze)
    asyncio.run(worker.process_corpus_analysis_job(job_id))

    with TestingSessionLocal() as db:
        saved_job = db.get(WebsiteCorpusAnalysisJob, job_id)
        analysis = db.get(WebsiteCorpusAnalysis, website_id)
        assert saved_job is not None
        assert saved_job.status == "succeeded"
        assert analysis is not None
        assert analysis.words[0]["word"] == "home services"

    response = client.get(
        "/api/v1/websites/corpus-words",
        params={"website_ids": str(website_id)},
    )
    assert response.status_code == 200
    assert response.json()["websites"][0]["words"][0]["word"] == (
        "home services"
    )
    assert response.json()["pending_website_ids"] == []


def test_failed_keyword_job_is_visible_and_can_be_retried(
    client,
    monkeypatch,
):
    _disable_keyword_dispatch(monkeypatch)
    crawled_at = datetime(2026, 11, 14, tzinfo=timezone.utc)
    with TestingSessionLocal() as db:
        website = _create_crawled_website(
            db,
            "unavailable.example",
            crawled_at=crawled_at,
        )
        website_id = website.id
        job = WebsiteCorpusAnalysisJob(
            website_id=website_id,
            source_crawled_at=crawled_at,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    def fail_analysis(_sites):
        raise HTTPException(
            status_code=502,
            detail="The crawler service rejected the corpus analysis request.",
        )

    monkeypatch.setattr(worker, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(worker, "_analyze_sites", fail_analysis)
    asyncio.run(worker.process_corpus_analysis_job(job_id))

    response = client.get(
        "/api/v1/websites/corpus-words",
        params={"website_ids": str(website_id)},
    )
    assert response.status_code == 200
    assert response.json()["failed_website_ids"] == [str(website_id)]

    retry = client.post(
        f"/api/v1/websites/corpus-words/jobs/{job_id}/retry",
    )
    assert retry.status_code == 202
    assert retry.json()["status"] == "queued"

    with TestingSessionLocal() as db:
        retried_job = db.get(WebsiteCorpusAnalysisJob, job_id)
        assert retried_job is not None
        retried_job.status = "failed"
        db.commit()
    retry_batch = client.post(
        "/api/v1/websites/corpus-words/jobs/retry-batch",
        json={"job_ids": [str(job_id)]},
    )
    assert retry_batch.status_code == 202
    assert retry_batch.json()["retried_job_ids"] == [str(job_id)]


def test_successful_crawl_creates_keyword_and_ahrefs_jobs_in_database(
    client,
    monkeypatch,
):
    crawled_at = datetime(2026, 11, 14, tzinfo=timezone.utc)
    website_url = "https://example.com/"
    with TestingSessionLocal() as db:
        crawl_job = WebsiteCrawlJob(
            website_url=website_url,
            status="running",
        )
        db.add(crawl_job)
        db.commit()
        crawl_job_id = crawl_job.id

    def persist_crawl(_crawl_data, db):
        website = _create_crawled_website(
            db,
            "example.com",
            crawled_at=crawled_at,
        )
        return db.get(WebsiteCrawl, website.crawl_id)

    monkeypatch.setattr(worker, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(worker, "_persist_crawl", persist_crawl)
    monkeypatch.setattr(worker, "create_website_pagespeed_jobs", lambda *_: [])

    _, corpus_job_ids, ahrefs_job_ids = worker._save_success(
        crawl_job_id,
        CrawlData(
            website_url=website_url,
            about_page_url=None,
            about_content=None,
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
        ),
    )

    assert len(corpus_job_ids) == 1
    assert len(ahrefs_job_ids) == 1
    with TestingSessionLocal() as db:
        saved_job = db.get(WebsiteCorpusAnalysisJob, corpus_job_ids[0])
        assert saved_job is not None
        assert saved_job.status == "queued"
        ahrefs_job = db.get(WebsiteAhrefsDomainRatingJob, ahrefs_job_ids[0])
        assert ahrefs_job is not None
        assert ahrefs_job.status == "queued"
        saved_crawl_job = db.get(WebsiteCrawlJob, crawl_job_id)
        assert saved_crawl_job is not None
        assert saved_crawl_job.status == "succeeded"
