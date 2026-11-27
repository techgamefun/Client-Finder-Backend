import asyncio

from sqlalchemy import select

from app.models import Contact, PageSpeedScanJob, Website, WebsiteCrawl
from app.schemas.discovery import DiscoveryResult, DiscoverySearchResponse
from app.services import discovery_ingestion
from app.services.website_crawler import CrawlData
from conftest import TestingSessionLocal


def _search_response(*urls: str) -> DiscoverySearchResponse:
    return DiscoverySearchResponse(
        query="manufacturing websites",
        results=[
            DiscoveryResult(
                title=f"Business {index}",
                url=url,
                content="Business website",
                score=0.9,
            )
            for index, url in enumerate(urls)
        ],
    )


def _mock_crawls(monkeypatch, *, emails: list[str] | None = None):
    async def fake_crawl(website_url: str, **_options):
        return CrawlData(
            website_url=website_url.split("?", maxsplit=1)[0],
            about_page_url=None,
            about_content=None,
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
            emails=emails or [],
            phones=[],
        )

    async def fake_enqueue(_job_ids):
        return None

    monkeypatch.setattr(discovery_ingestion, "crawl_website", fake_crawl)
    monkeypatch.setattr(discovery_ingestion, "enqueue_pagespeed_jobs", fake_enqueue)


def test_ingestion_saves_websites_contacts_and_website_owned_pagespeed_jobs(
    client,
    monkeypatch,
):
    async def fake_search(**_kwargs):
        return _search_response(
            "https://example.com/about",
            "https://example.com/contact",
        )

    monkeypatch.setattr(discovery_ingestion, "search_websites", fake_search)
    _mock_crawls(monkeypatch, emails=["hello@example.com", "HELLO@example.com"])

    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing websites"},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["websites_created"] == 1
    assert result["websites_crawled"] == 2
    assert result["contacts_added"] == 1
    assert result["pagespeed_scans_queued"] == 2
    assert {job["strategy"] for job in result["pagespeed_jobs"]} == {
        "mobile",
        "desktop",
    }

    with TestingSessionLocal() as db:
        websites = db.scalars(select(Website)).all()
        contacts = db.scalars(select(Contact)).all()
        jobs = db.scalars(select(PageSpeedScanJob)).all()
    assert len(websites) == 1
    assert websites[0].domain == "example.com"
    assert websites[0].business_name == "Business 0"
    assert websites[0].status == "needs_review"
    assert len(contacts) == 1
    assert contacts[0].website_id == websites[0].id
    assert contacts[0].business_email == "hello@example.com"
    assert len(jobs) == 2
    assert all(job.website_id == websites[0].id for job in jobs)


def test_ingestion_deduplicates_result_urls_and_registry_domains(
    client,
    monkeypatch,
):
    response = _search_response(
        "https://example.com/first",
        "https://example.com/second",
    )
    async def fake_search(**_kwargs):
        return response

    monkeypatch.setattr(discovery_ingestion, "search_websites", fake_search)
    _mock_crawls(monkeypatch)

    first = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing websites"},
    )
    second = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing websites"},
    )

    assert first.status_code == 200, first.text
    assert first.json()["websites_created"] == 1
    assert second.status_code == 200, second.text
    assert second.json()["websites_created"] == 0
    with TestingSessionLocal() as db:
        assert len(db.scalars(select(Website)).all()) == 1


def test_ingestion_crawls_at_most_ten_websites_concurrently(client, monkeypatch):
    async def fake_search(**_kwargs):
        return _search_response(
            *(f"https://site-{index}.example/" for index in range(10))
        )

    monkeypatch.setattr(discovery_ingestion, "search_websites", fake_search)
    active_crawls = 0
    peak_crawls = 0

    async def fake_crawl(website_url: str, **_options):
        nonlocal active_crawls, peak_crawls
        active_crawls += 1
        peak_crawls = max(peak_crawls, active_crawls)
        try:
            await asyncio.sleep(0.01)
        finally:
            active_crawls -= 1
        return CrawlData(
            website_url=website_url,
            about_page_url=None,
            about_content=None,
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
            emails=[],
            phones=[],
        )

    async def fake_enqueue(_job_ids):
        return None

    monkeypatch.setattr(discovery_ingestion, "crawl_website", fake_crawl)
    monkeypatch.setattr(discovery_ingestion, "enqueue_pagespeed_jobs", fake_enqueue)

    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing websites"},
    )

    assert response.status_code == 200, response.text
    assert peak_crawls == 10
    assert response.json()["websites_created"] == 10


def test_ingestion_reports_crawl_failure_without_losing_website(client, monkeypatch):
    from fastapi import HTTPException

    async def fake_search(**_kwargs):
        return _search_response("https://example.com/about")

    async def fail_crawl(website_url: str, **_options):
        raise HTTPException(status_code=502, detail="Website unavailable.")

    async def fake_enqueue(_job_ids):
        return None

    monkeypatch.setattr(discovery_ingestion, "search_websites", fake_search)
    monkeypatch.setattr(discovery_ingestion, "crawl_website", fail_crawl)
    monkeypatch.setattr(discovery_ingestion, "enqueue_pagespeed_jobs", fake_enqueue)

    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing websites"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["websites_crawled"] == 0
    assert response.json()["crawl_failures"] == [
        {
            "source_url": "https://example.com/about",
            "detail": "Website unavailable.",
        }
    ]
    with TestingSessionLocal() as db:
        assert db.scalar(select(Website).where(Website.domain == "example.com"))
