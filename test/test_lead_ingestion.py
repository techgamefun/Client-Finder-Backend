import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from conftest import TestingSessionLocal
from app.models import (
    Company,
    Contact,
    PageSpeedScan,
    PageSpeedScanJob,
    SearchKeyword,
    UserProviderCredential,
    Website,
    WebsiteCrawl,
)
from app.schemas.discovery import DiscoveryResult, DiscoverySearchResponse
from app.services import lead_ingestion
from app.services.website_crawler import CrawlData


def _save_pagespeed_key(db: Session) -> None:
    context = db.info["tenant_context"]
    db.add(
        UserProviderCredential(
            user_id=context.user_id,
            provider="pagespeed",
            api_key="test-pagespeed-key",
        )
    )


def _search_response(*results: tuple[str, str, str]) -> DiscoverySearchResponse:
    return DiscoverySearchResponse(
        query="manufacturing companies",
        results=[
            DiscoveryResult(
                title=title,
                url=url,
                content=evidence,
                score=0.9,
            )
            for title, url, evidence in results
        ],
    )


def _mock_search(monkeypatch, response: DiscoverySearchResponse) -> None:
    async def fake_search(query: str, max_results: int, db):
        return response

    async def fake_crawl(website_url: str, **_options):
        return CrawlData(
            website_url=website_url.split("?", maxsplit=1)[0],
            about_page_url=None,
            about_content=None,
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
            emails=[],
            social_links=[],
        )

    monkeypatch.setattr(lead_ingestion, "search_companies", fake_search)
    monkeypatch.setattr(lead_ingestion, "crawl_website", fake_crawl)
    monkeypatch.setattr(
        lead_ingestion,
        "enqueue_pagespeed_jobs",
        _fake_enqueue_pagespeed_jobs,
    )


async def _fake_enqueue_pagespeed_jobs(_job_ids) -> None:
    return None


def test_ingestion_deduplicates_company_name_and_source_url(client, monkeypatch):
    response = _search_response(
        ("Acme, Inc.", "https://example.com/acme", "Acme evidence"),
        (" ACME Inc ", "https://example.com/acme/about", "More evidence"),
        ("Another Company", "https://example.com/acme", "Repeated URL"),
    )
    _mock_search(monkeypatch, response)

    result = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert result.status_code == 200
    response_body = result.json()
    jobs = response_body.pop("pagespeed_jobs")
    assert response_body == {
        "query": "manufacturing companies",
        "results_found": 3,
        "candidates_created": 1,
        "results_skipped": 1,
        "websites_crawled": 2,
        "companies_enriched": 1,
        "contacts_added": 0,
        "crawl_failures": [],
        "pagespeed_scans_queued": 2,
    }
    assert len(jobs) == 2
    assert {job["strategy"] for job in jobs} == {"mobile", "desktop"}
    assert {job["status"] for job in jobs} == {"queued"}

    with TestingSessionLocal() as db:
        companies = db.scalars(select(Company)).all()
        websites = db.scalars(select(Website)).all()
        scans = db.scalars(select(PageSpeedScan)).all()
        scan_jobs = db.scalars(select(PageSpeedScanJob)).all()
        search_keywords = [
            (search.keyword, search.searched_at)
            for search in websites[0].search_keywords
        ]

    assert len(companies) == 1
    assert companies[0].normalized_name == "acme inc"
    assert companies[0].lead_status == "needs_review"
    assert len(websites) == 1
    assert scans == []
    assert len(scan_jobs) == 2
    assert {job.strategy for job in scan_jobs} == {"mobile", "desktop"}
    assert len(search_keywords) == 1
    assert search_keywords[0][0] == "manufacturing companies"
    assert search_keywords[0][1] is not None
    company_record = client.get(f"/api/v1/companies/{companies[0].id}")
    assert company_record.status_code == 200
    assert "search_keywords" not in company_record.json()
    assert "sources" not in company_record.json()
    assert company_record.json()["pagespeed_scans"] == []


def test_reingesting_existing_result_keeps_one_company(client, monkeypatch):
    response = _search_response(
        ("Example Company", "https://example.com/company", "Evidence"),
    )
    _mock_search(monkeypatch, response)

    first = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )
    second = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["candidates_created"] == 0
    assert second.json()["results_skipped"] == 0
    assert second.json()["websites_crawled"] == 1
    assert second.json()["pagespeed_scans_queued"] == 2

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 1
        website = db.scalar(select(Website))
        assert website is not None
        assert len(website.search_keywords) == 2
        assert db.scalar(select(func.count()).select_from(PageSpeedScanJob)) == 4
        assert db.scalar(select(func.count()).select_from(PageSpeedScan)) == 0


def test_ingestion_shares_jobs_for_companies_with_same_website(
    client,
    monkeypatch,
):
    _mock_search(
        monkeypatch,
        _search_response(
            ("First Example", "https://example.com/first", "First"),
            ("Second Example", "https://example.com/second", "Second"),
        ),
    )

    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert response.status_code == 200
    jobs = response.json()["pagespeed_jobs"]
    assert len(jobs) == 2
    assert {job["website_url"] for job in jobs} == {"https://example.com/"}
    assert {job["strategy"] for job in jobs} == {"mobile", "desktop"}
    assert len(jobs[0]["company_ids"]) == 2
    assert set(jobs[0]["company_ids"]) == set(jobs[1]["company_ids"])


def test_ingestion_links_the_original_search_timestamp(client, monkeypatch):
    _mock_search(
        monkeypatch,
        _search_response(
            ("Example Company", "https://example.com/company", "Evidence"),
        ),
    )
    searched_at = datetime(2026, 11, 9, 12, 0)
    with TestingSessionLocal() as db:
        search = SearchKeyword(
            keyword="manufacturing companies",
            searched_at=searched_at,
        )
        db.add(search)
        db.commit()
        search_id = search.id

    response = client.post(
        "/api/v1/discovery/ingest",
        json={
            "query": "manufacturing companies",
            "search_id": str(search_id),
        },
    )
    repeated_response = client.post(
        "/api/v1/discovery/ingest",
        json={
            "query": "manufacturing companies",
            "search_id": str(search_id),
        },
    )

    assert response.status_code == 200
    assert repeated_response.status_code == 200
    with TestingSessionLocal() as db:
        website = db.scalar(select(Website))
        assert website is not None
        assert len(website.search_keywords) == 1
        assert website.search_keywords[0].id == search_id
        assert website.search_keywords[0].searched_at == searched_at


def test_ingestion_attaches_new_source_to_existing_company(client, monkeypatch):
    created = client.post(
        "/api/v1/companies",
        json={
            "name": "Example Manufacturing, Inc.",
            "website": "https://existing.example",
            "domain": "existing.example",
            "industry": "Manufacturing",
        },
    )
    assert created.status_code == 201

    _mock_search(
        monkeypatch,
        _search_response(
            (
                " example manufacturing inc ",
                "https://example.com/manufacturing",
                "Search evidence",
            ),
        ),
    )
    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert response.status_code == 200
    assert response.json()["candidates_created"] == 0

    with TestingSessionLocal() as db:
        companies = db.scalars(select(Company)).all()
        website = db.scalar(
            select(Website).where(Website.domain == "example.com")
        )
        website_keywords = [
            keyword.keyword for keyword in website.search_keywords
        ] if website is not None else []

    assert len(companies) == 1
    assert companies[0].lead_status == "new"
    assert companies[0].website == "https://existing.example/"
    assert companies[0].domain == "existing.example"
    assert companies[0].industry == "Manufacturing"
    assert website is not None
    assert website_keywords == ["manufacturing companies"]


def test_ingestion_uses_crawl_to_fill_missing_company_details(
    client,
    monkeypatch,
):
    _mock_search(
        monkeypatch,
        _search_response(
            ("Example Company", "https://example.com/about", "Search evidence"),
        ),
    )

    async def fake_crawl(website_url: str, **_options):
        return CrawlData(
            website_url=website_url,
            about_page_url="https://example.com/about",
            about_content="About the company.",
            contact_page_url="https://example.com/contact",
            contact_content="Contact hello@example.com.",
            goals_page_url=None,
            goals_content=None,
            emails=["hello@example.com", "HELLO@example.com"],
            phones=["+1 (555) 0100", "15550100"],
            social_links=["https://www.linkedin.com/company/example"],
        )

    monkeypatch.setattr(lead_ingestion, "crawl_website", fake_crawl)

    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert response.status_code == 200
    assert response.json()["websites_crawled"] == 1
    assert response.json()["companies_enriched"] == 1
    assert response.json()["contacts_added"] == 2
    assert response.json()["crawl_failures"] == []
    assert response.json()["pagespeed_scans_queued"] == 2

    with TestingSessionLocal() as db:
        company = db.query(Company).one()
        contacts = db.query(Contact).all()
        crawl = db.query(WebsiteCrawl).one()

    assert company.website == "https://example.com/"
    assert company.domain == "example.com"
    assert len(contacts) == 2
    email_contact = next(contact for contact in contacts if contact.business_email)
    phone_contact = next(contact for contact in contacts if contact.business_phone)
    assert email_contact.business_email == "hello@example.com"
    assert email_contact.business_phone is None
    assert email_contact.is_publicly_listed is True
    assert phone_contact.business_phone == "+1 (555) 0100"
    assert phone_contact.business_email is None
    assert phone_contact.is_publicly_listed is True
    assert crawl.about_content == "About the company."
    assert crawl.phones == ["+1 (555) 0100", "15550100"]


def test_failed_crawl_is_reported_while_saving_the_source(
    client,
    monkeypatch,
):
    _mock_search(
        monkeypatch,
        _search_response(
            ("Example Company", "https://example.com/about", "Search evidence"),
        ),
    )

    async def fail_crawl(_website_url: str, **_options):
        raise HTTPException(status_code=502, detail="Website unavailable.")

    monkeypatch.setattr(lead_ingestion, "crawl_website", fail_crawl)

    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert response.status_code == 200
    assert response.json()["websites_crawled"] == 0
    assert response.json()["crawl_failures"] == [
        {
            "source_url": "https://example.com/about",
            "detail": "Website unavailable.",
        }
    ]
    assert response.json()["pagespeed_scans_queued"] == 2


def test_ingestion_crawls_the_submitted_search_result_url(
    client,
    monkeypatch,
):
    async def unexpected_search(query: str, max_results: int, db):
        raise AssertionError("Ingestion should use the submitted results.")

    crawled_urls: list[str] = []

    async def fake_crawl(website_url: str, **_options):
        crawled_urls.append(website_url)
        return CrawlData(
            website_url=website_url,
            about_page_url=None,
            about_content=None,
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
            emails=[],
            social_links=[],
        )

    monkeypatch.setattr(lead_ingestion, "search_companies", unexpected_search)
    monkeypatch.setattr(lead_ingestion, "crawl_website", fake_crawl)
    source_url = "https://example.com/company/about"

    response = client.post(
        "/api/v1/discovery/ingest",
        json={
            "query": "manufacturing companies",
            "results": [
                {
                    "title": "Example Company",
                    "url": source_url,
                    "content": "Company profile",
                    "score": 0.9,
                }
            ],
        },
    )

    assert response.status_code == 200
    assert crawled_urls == [source_url]
    assert response.json()["websites_crawled"] == 1
    assert response.json()["pagespeed_scans_queued"] == 2


def test_ingestion_crawls_up_to_ten_websites_concurrently(client, monkeypatch):
    results = _search_response(
        *[
            (f"Company {index}", f"https://company{index}.example", "Evidence")
            for index in range(10)
        ]
    )
    _mock_search(monkeypatch, results)
    active_crawls = 0
    peak_crawls = 0

    async def track_crawl(website_url: str, **_options):
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
            social_links=[],
        )

    monkeypatch.setattr(lead_ingestion, "crawl_website", track_crawl)

    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert response.status_code == 200
    assert peak_crawls == 10


def test_ingestion_queues_pagespeed_jobs_without_waiting(client, monkeypatch):
    _mock_search(
        monkeypatch,
        _search_response(
            ("Example Company", "https://example.com/about", "Search evidence"),
        ),
    )
    enqueued_ids: list[str] = []

    async def capture_enqueued_jobs(job_ids):
        enqueued_ids.extend(str(job_id) for job_id in job_ids)

    monkeypatch.setattr(
        lead_ingestion,
        "enqueue_pagespeed_jobs",
        capture_enqueued_jobs,
    )

    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert response.status_code == 200
    assert response.json()["pagespeed_scans_queued"] == 2
    jobs = response.json()["pagespeed_jobs"]
    assert enqueued_ids == [job["id"] for job in jobs]
    assert {job["strategy"] for job in jobs} == {"mobile", "desktop"}
    assert all(job["status"] == "queued" for job in jobs)
    assert all(job["started_at"] is None for job in jobs)


def test_pagespeed_queue_endpoint_lists_jobs(client):
    with TestingSessionLocal() as db:
        company = Company(
            name="Example Company",
            normalized_name="example company",
            website="https://example.com/",
        )
        db.add(company)
        db.flush()
        job = PageSpeedScanJob(
            company_id=company.id,
            website_url="https://example.com/",
            strategy="mobile",
            status="queued",
            companies=[company],
        )
        db.add(job)
        db.commit()
        job_id = job.id
        company_id = company.id

    response = client.get("/api/v1/pagespeed/jobs")

    assert response.status_code == 200
    result = response.json()
    jobs = result["items"]
    assert len(jobs) == 1
    assert jobs[0]["id"] == str(job_id)
    assert jobs[0]["website_url"] == "https://example.com/"
    assert jobs[0]["company_ids"] == [str(company_id)]
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["started_at"] is None
    assert result["total_count"] == 1
    assert result["status_counts"] == {"queued": 1}


def test_pagespeed_jobs_search_status_and_pagination(client):
    with TestingSessionLocal() as db:
        jobs = []
        for domain, strategy, job_status in (
            ("alpha-one.example", "mobile", "queued"),
            ("alpha-two.example", "desktop", "queued"),
            ("alpha-three.example", "mobile", "failed"),
            ("beta.example", "mobile", "queued"),
        ):
            website_url = f"https://{domain}/"
            company = Company(name=domain, website=website_url)
            db.add(company)
            db.flush()
            jobs.append(
                PageSpeedScanJob(
                    company_id=company.id,
                    website_url=website_url,
                    strategy=strategy,
                    status=job_status,
                )
            )
        db.add_all(jobs)
        db.commit()
        expected_job_ids = {str(job.id) for job in jobs[:2]}

    response = client.get(
        "/api/v1/pagespeed/jobs",
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


def test_failed_pagespeed_job_can_be_retried(client, monkeypatch):
    enqueued_ids: list[UUID] = []

    async def fake_enqueue(job_ids: list[UUID]) -> None:
        enqueued_ids.extend(job_ids)

    monkeypatch.setattr(
        "app.services.pagespeed_jobs.enqueue_pagespeed_jobs",
        fake_enqueue,
    )
    with TestingSessionLocal() as db:
        _save_pagespeed_key(db)
        company = Company(
            name="Example Company",
            normalized_name="example company",
            website="https://example.com/",
        )
        db.add(company)
        db.flush()
        job = PageSpeedScanJob(
            company_id=company.id,
            website_url="https://example.com/",
            strategy="mobile",
            status="failed",
            attempts=3,
            error="PageSpeed request failed.",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            completed_at=datetime.now(timezone.utc),
            companies=[company],
        )
        db.add(job)
        db.commit()
        job_id = job.id

    response = client.post(f"/api/v1/pagespeed/jobs/{job_id}/retry")

    assert response.status_code == 202
    retried = response.json()
    assert retried["status"] == "queued"
    assert retried["attempts"] == 0
    assert retried["error"] is None
    assert retried["started_at"] is None
    assert retried["completed_at"] is None
    assert enqueued_ids == [job_id]


def test_failed_pagespeed_job_cannot_be_retried_without_api_key(
    client,
    monkeypatch,
):
    enqueued_ids: list[UUID] = []

    async def capture_enqueue(job_ids: list[UUID]) -> None:
        enqueued_ids.extend(job_ids)

    monkeypatch.setattr(
        "app.services.pagespeed_jobs.enqueue_pagespeed_jobs",
        capture_enqueue,
    )
    with TestingSessionLocal() as db:
        company = Company(
            name="Example Company",
            normalized_name="example company",
            website="https://example.com/",
        )
        db.add(company)
        db.flush()
        job = PageSpeedScanJob(
            company_id=company.id,
            website_url="https://example.com/",
            strategy="mobile",
            status="failed",
            error="PageSpeed request failed.",
            companies=[company],
        )
        db.add(job)
        db.commit()
        job_id = job.id

    response = client.post(f"/api/v1/pagespeed/jobs/{job_id}/retry")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "The user who created this PageSpeed job must configure a Google "
        "PageSpeed API key before it can be queued."
    )
    assert enqueued_ids == []


def test_batch_retry_pagespeed_jobs_updates_and_dispatches_once(
    client,
    monkeypatch,
):
    dispatch_calls: list[list[UUID]] = []

    async def capture_enqueue(job_ids: list[UUID]) -> None:
        dispatch_calls.append(job_ids)

    monkeypatch.setattr(
        "app.services.pagespeed_jobs.enqueue_pagespeed_jobs",
        capture_enqueue,
    )
    now = datetime.now(timezone.utc)
    with TestingSessionLocal() as db:
        _save_pagespeed_key(db)
        website = Website(
            website_url="https://example.com/",
            domain="example.com",
        )
        db.add(website)
        db.flush()
        failed_jobs = [
            PageSpeedScanJob(
                website_id=website.id,
                website_url=website.website_url,
                strategy=strategy,
                status="failed",
                attempts=2,
                error="PageSpeed request failed.",
                started_at=now,
                completed_at=now,
            )
            for strategy in ("mobile", "desktop")
        ]
        queued_job = PageSpeedScanJob(
            website_id=website.id,
            website_url=website.website_url,
            strategy="mobile",
            status="queued",
        )
        db.add_all([*failed_jobs, queued_job])
        db.commit()
        failed_ids = [job.id for job in failed_jobs]
        queued_id = queued_job.id

    response = client.post(
        "/api/v1/pagespeed/jobs/retry-batch",
        json={"job_ids": [str(job_id) for job_id in [*failed_ids, queued_id]]},
    )

    assert response.status_code == 202
    result = response.json()
    assert result["retried_job_ids"] == [str(job_id) for job_id in failed_ids]
    assert result["failures"] == [
        {
            "job_id": str(queued_id),
            "message": "Only failed PageSpeed scan jobs can be retried.",
        }
    ]
    assert dispatch_calls == [failed_ids]

    with TestingSessionLocal() as db:
        retried_jobs = [db.get(PageSpeedScanJob, job_id) for job_id in failed_ids]
        assert all(job is not None for job in retried_jobs)
        assert all(job.status == "queued" for job in retried_jobs)
        assert all(job.attempts == 0 for job in retried_jobs)
        assert all(job.error is None for job in retried_jobs)
        assert all(job.started_at is None for job in retried_jobs)
        assert all(job.completed_at is None for job in retried_jobs)


def test_ingestion_rolls_back_whole_batch_on_commit_failure(
    client,
    monkeypatch,
):
    response = _search_response(
        ("First Company", "https://example.com/first", "First"),
        ("Second Company", "https://example.com/second", "Second"),
    )
    _mock_search(monkeypatch, response)

    def fail_commit(session: Session) -> None:
        raise RuntimeError("database commit failed")

    event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="database commit failed"):
            client.post(
                "/api/v1/discovery/ingest",
                json={"query": "manufacturing companies"},
            )
    finally:
        event.remove(Session, "before_commit", fail_commit)

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 0
        assert db.scalar(select(func.count()).select_from(SearchKeyword)) == 0
        assert db.scalar(select(func.count()).select_from(PageSpeedScan)) == 0
