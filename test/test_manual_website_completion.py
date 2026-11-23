from uuid import UUID

from sqlalchemy import select

from conftest import TestingSessionLocal
from app.models import (
    PageSpeedScanJob,
    UserProviderCredential,
    Website,
    WebsiteAhrefsDomainRatingJob,
    WebsiteCrawl,
    WebsiteCrawlJob,
    WebsiteHypeStatFetchJob,
)
from app.services import manual_website_completion
from app.services.website_crawler import CrawlData, _persist_crawl


def _create_failed_crawl_job() -> UUID:
    with TestingSessionLocal() as db:
        context = db.info["tenant_context"]
        job = WebsiteCrawlJob(
            website_url="https://manual.example/",
            status="failed",
            error="Crawler service timed out.",
            created_by_user_id=context.user_id,
        )
        db.add(job)
        db.commit()
        return job.id


def _set_provider_credentials() -> None:
    with TestingSessionLocal() as db:
        user_id = db.info["tenant_context"].user_id
        db.add_all(
            [
                UserProviderCredential(
                    user_id=user_id,
                    provider=provider,
                    api_key=f"test-{provider}-key",
                )
                for provider in ("ahrefs", "hypestat", "pagespeed")
            ]
        )
        db.commit()


def test_manual_completion_saves_data_and_queues_configured_enrichment(
    client,
    monkeypatch,
):
    job_id = _create_failed_crawl_job()
    _set_provider_credentials()
    enqueued = {"ahrefs": [], "hypestat": [], "pagespeed": []}

    async def capture_ahrefs(job_ids):
        enqueued["ahrefs"].extend(job_ids)

    async def capture_hypestat(job_ids):
        enqueued["hypestat"].extend(job_ids)

    async def capture_pagespeed(job_ids):
        enqueued["pagespeed"].extend(job_ids)

    monkeypatch.setattr(
        manual_website_completion,
        "enqueue_website_ahrefs_domain_rating_jobs",
        capture_ahrefs,
    )
    monkeypatch.setattr(
        manual_website_completion,
        "enqueue_hypestat_fetch_jobs",
        capture_hypestat,
    )
    monkeypatch.setattr(
        manual_website_completion,
        "enqueue_pagespeed_jobs",
        capture_pagespeed,
    )

    response = client.post(
        f"/api/v1/crawler/jobs/{job_id}/manual-completion",
        json={
            "site_type": "B2B SaaS",
            "website_topics": ["analytics", "automation"],
            "language": "English",
            "home_page_url": "https://manual.example/",
            "home_content": "Reporting tools for growing teams.",
            "emails": ["hello@manual.example"],
            "phones": ["+1 555 010 1234"],
            "social_links": ["https://linkedin.com/company/manual-example"],
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "manual"
    assert result["ahrefs_domain_rating"] == "queued"
    assert result["hypestat"] == "queued"
    assert result["pagespeed"] == "queued"
    assert len(enqueued["ahrefs"]) == 1
    assert len(enqueued["hypestat"]) == 1
    assert len(enqueued["pagespeed"]) == 2

    with TestingSessionLocal() as db:
        job = db.get(WebsiteCrawlJob, job_id)
        assert job is not None
        assert job.status == "manual"
        assert job.error == "Crawler service timed out."

        crawl = db.get(WebsiteCrawl, job.crawl_id)
        assert crawl is not None
        assert crawl.source == "manual"
        assert crawl.site_type == "B2B SaaS"
        assert crawl.home_content == "Reporting tools for growing teams."
        assert crawl.emails == ["hello@manual.example"]

        website = db.scalar(
            select(Website).where(Website.domain == "manual.example")
        )
        assert website is not None
        assert website.category == "B2B SaaS"
        assert website.site_topic == ["analytics", "automation"]
        assert website.language == "English"

        assert len(
            db.scalars(
                select(WebsiteAhrefsDomainRatingJob).where(
                    WebsiteAhrefsDomainRatingJob.website_id == website.id
                )
            ).all()
        ) == 1
        assert len(
            db.scalars(
                select(WebsiteHypeStatFetchJob).where(
                    WebsiteHypeStatFetchJob.website_id == website.id
                )
            ).all()
        ) == 1
        assert len(
            db.scalars(
                select(PageSpeedScanJob).where(
                    PageSpeedScanJob.website_id == website.id
                )
            ).all()
        ) == 2

    queued_jobs = client.get("/api/v1/crawler/jobs?status=manual")
    assert queued_jobs.status_code == 200
    assert queued_jobs.json()["status_counts"]["manual"] == 1
    assert queued_jobs.json()["items"][0]["error"] == "Crawler service timed out."

    already_completed = client.post(
        f"/api/v1/crawler/jobs/{job_id}/manual-completion",
        json={"site_type": "B2B SaaS"},
    )
    assert already_completed.status_code == 409


def test_manual_completion_succeeds_when_enrichment_keys_are_missing(
    client,
    monkeypatch,
):
    job_id = _create_failed_crawl_job()

    async def no_enqueue(_job_ids):
        return None

    monkeypatch.setattr(
        manual_website_completion,
        "enqueue_website_ahrefs_domain_rating_jobs",
        no_enqueue,
    )
    monkeypatch.setattr(
        manual_website_completion,
        "enqueue_hypestat_fetch_jobs",
        no_enqueue,
    )
    monkeypatch.setattr(
        manual_website_completion,
        "enqueue_pagespeed_jobs",
        no_enqueue,
    )

    response = client.post(
        f"/api/v1/crawler/jobs/{job_id}/manual-completion",
        json={"site_type": "Local business"},
    )

    assert response.status_code == 200
    assert response.json()["ahrefs_domain_rating"] == "not_configured"
    assert response.json()["hypestat"] == "not_configured"
    assert response.json()["pagespeed"] == "not_configured"

    with TestingSessionLocal() as db:
        job = db.get(WebsiteCrawlJob, job_id)
        assert job is not None
        assert job.status == "manual"
        assert job.error == "Crawler service timed out."
        assert db.scalar(select(WebsiteAhrefsDomainRatingJob.id)) is None
        assert db.scalar(select(WebsiteHypeStatFetchJob.id)) is None
        assert db.scalar(select(PageSpeedScanJob.id)) is None


def test_successful_crawl_updates_manual_crawl_source(client):
    with TestingSessionLocal() as db:
        db.add(
            WebsiteCrawl(
                website_url="https://manual.example/",
                source="manual",
            )
        )
        db.commit()
        crawl = _persist_crawl(
            CrawlData(
                website_url="https://manual.example/",
                about_page_url=None,
                about_content=None,
                contact_page_url=None,
                contact_content=None,
                goals_page_url=None,
                goals_content=None,
            ),
            db,
        )

    assert crawl.source == "crawler"
