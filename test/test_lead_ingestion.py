from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from conftest import TestingSessionLocal
from app.models import (
    Company,
    Contact,
    LeadSource,
    PageSpeedScan,
    SearchKeyword,
    WebsiteCrawl,
)
from app.schemas.discovery import DiscoveryResult, DiscoverySearchResponse
from app.services import lead_ingestion
from app.services.pagespeed import PageSpeedError, PageSpeedScanData
from app.services.website_crawler import CrawlData


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
    async def fake_search(query: str, max_results: int):
        return response

    async def fake_crawl(website_url: str):
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

    async def fake_pagespeed(website_url: str, strategy: str):
        return PageSpeedScanData(
            website_url=website_url,
            strategy=strategy,
            category_scores={
                "performance": 91,
                "accessibility": 92,
                "best-practices": 93,
                "seo": 94,
            },
            report={
                "lighthouseResult": {
                    "lighthouseVersion": "12.0.0",
                    "audits": {
                        "first-contentful-paint": {
                            "title": "First Contentful Paint",
                            "score": 0.9,
                            "displayValue": "1.2 s",
                        }
                    },
                }
            },
        )

    monkeypatch.setattr(lead_ingestion, "search_companies", fake_search)
    monkeypatch.setattr(lead_ingestion, "crawl_website", fake_crawl)
    monkeypatch.setattr(lead_ingestion, "scan_pagespeed", fake_pagespeed)


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
    assert result.json() == {
        "query": "manufacturing companies",
        "results_found": 3,
        "candidates_created": 1,
        "sources_added": 2,
        "results_skipped": 1,
        "websites_crawled": 2,
        "companies_enriched": 1,
        "contacts_added": 0,
        "crawl_failures": [],
        "pagespeed_scans_saved": 2,
        "pagespeed_failures": [],
    }

    with TestingSessionLocal() as db:
        companies = db.scalars(select(Company)).all()
        sources = db.scalars(select(LeadSource)).all()
        scans = db.scalars(select(PageSpeedScan)).all()
        search_keywords = [
            (search.keyword, search.searched_at)
            for search in companies[0].search_keywords
        ]

    assert len(companies) == 1
    assert companies[0].normalized_name == "acme inc"
    assert companies[0].lead_status == "needs_review"
    assert len(sources) == 2
    assert len(scans) == 2
    assert {scan.strategy for scan in scans} == {"mobile", "desktop"}
    assert {source.company_id for source in sources} == {companies[0].id}
    assert len(search_keywords) == 1
    assert search_keywords[0][0] == "manufacturing companies"
    assert search_keywords[0][1] is not None
    company_record = client.get(f"/api/v1/companies/{companies[0].id}")
    assert company_record.status_code == 200
    assert company_record.json()["search_keywords"][0]["keyword"] == (
        "manufacturing companies"
    )
    saved_scans = company_record.json()["pagespeed_scans"]
    assert len(saved_scans) == 2
    detailed_scan = client.get(
        f"/api/v1/companies/{companies[0].id}/pagespeed-scans/"
        f"{saved_scans[0]['id']}"
    )
    assert detailed_scan.status_code == 200
    assert (
        detailed_scan.json()["report"]["lighthouseResult"]["audits"][
            "first-contentful-paint"
        ]["displayValue"]
        == "1.2 s"
    )


def test_reingesting_existing_source_skips_it(client, monkeypatch):
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
    assert first.json()["sources_added"] == 1
    assert second.status_code == 200
    assert second.json()["candidates_created"] == 0
    assert second.json()["sources_added"] == 0
    assert second.json()["results_skipped"] == 1
    assert second.json()["websites_crawled"] == 1
    assert second.json()["pagespeed_scans_saved"] == 2

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 1
        assert db.scalar(select(func.count()).select_from(LeadSource)) == 1
        assert db.scalar(select(func.count()).select_from(PageSpeedScan)) == 4


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
    assert repeated_response.json()["sources_added"] == 0
    with TestingSessionLocal() as db:
        company = db.scalar(select(Company))
        assert company is not None
        assert len(company.search_keywords) == 1
        assert company.search_keywords[0].id == search_id
        assert company.search_keywords[0].searched_at == searched_at


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
    assert response.json()["sources_added"] == 1

    with TestingSessionLocal() as db:
        companies = db.scalars(select(Company)).all()
        sources = db.scalars(select(LeadSource)).all()

    assert len(companies) == 1
    assert companies[0].lead_status == "new"
    assert companies[0].website == "https://existing.example/"
    assert companies[0].domain == "existing.example"
    assert companies[0].industry == "Manufacturing"
    assert len(sources) == 1
    assert sources[0].company_id == companies[0].id


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

    async def fake_crawl(website_url: str):
        return CrawlData(
            website_url=website_url,
            about_page_url="https://example.com/about",
            about_content="About the company.",
            contact_page_url="https://example.com/contact",
            contact_content="Contact hello@example.com.",
            goals_page_url=None,
            goals_content=None,
            emails=["hello@example.com", "HELLO@example.com"],
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
    assert response.json()["contacts_added"] == 1
    assert response.json()["crawl_failures"] == []
    assert response.json()["pagespeed_scans_saved"] == 2

    with TestingSessionLocal() as db:
        company = db.query(Company).one()
        contacts = db.query(Contact).all()
        source = db.query(LeadSource).one()
        crawl = db.query(WebsiteCrawl).one()

    assert company.website == "https://example.com/"
    assert company.domain == "example.com"
    assert len(contacts) == 1
    assert contacts[0].business_email == "hello@example.com"
    assert contacts[0].is_publicly_listed is True
    assert "About page:\nAbout the company." in source.evidence
    assert crawl.about_content == "About the company."


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

    async def fail_crawl(_website_url: str):
        raise HTTPException(status_code=502, detail="Website unavailable.")

    monkeypatch.setattr(lead_ingestion, "crawl_website", fail_crawl)

    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert response.status_code == 200
    assert response.json()["sources_added"] == 1
    assert response.json()["websites_crawled"] == 0
    assert response.json()["crawl_failures"] == [
        {
            "source_url": "https://example.com/about",
            "detail": "Website unavailable.",
        }
    ]
    assert response.json()["pagespeed_scans_saved"] == 2


def test_ingestion_crawls_the_submitted_search_result_url(
    client,
    monkeypatch,
):
    async def unexpected_search(query: str, max_results: int):
        raise AssertionError("Ingestion should use the submitted results.")

    crawled_urls: list[str] = []

    async def fake_crawl(website_url: str):
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
    assert response.json()["pagespeed_scans_saved"] == 2


def test_pagespeed_failures_do_not_prevent_pipeline_saves(client, monkeypatch):
    _mock_search(
        monkeypatch,
        _search_response(
            ("Example Company", "https://example.com/about", "Search evidence"),
        ),
    )

    async def fail_pagespeed(website_url: str, strategy: str):
        raise PageSpeedError("Google PageSpeed rejected the scan request.")

    monkeypatch.setattr(lead_ingestion, "scan_pagespeed", fail_pagespeed)

    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert response.status_code == 200
    assert response.json()["sources_added"] == 1
    assert response.json()["pagespeed_scans_saved"] == 0
    assert response.json()["pagespeed_failures"] == [
        {
            "website_url": "https://example.com/",
            "strategy": strategy,
            "detail": "Google PageSpeed rejected the scan request.",
        }
        for strategy in ("mobile", "desktop")
    ]


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
        assert db.scalar(select(func.count()).select_from(LeadSource)) == 0
        assert db.scalar(select(func.count()).select_from(PageSpeedScan)) == 0
