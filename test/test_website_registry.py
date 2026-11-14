from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from conftest import TestingSessionLocal
from app.models import (
    Company,
    PageSpeedScanJob,
    Website,
    WebsiteCrawl,
    WebsiteCrawlJob,
)
from app.schemas.discovery import (
    DiscoveryProvider,
    DiscoveryResult,
    DiscoverySearchResponse,
)
from app.services import lead_ingestion
from app.services.website_crawler import CrawlData


def test_websites_endpoint_returns_domain_and_crawl_details(client):
    with TestingSessionLocal() as db:
        crawl = WebsiteCrawl(
            website_url="https://example.com/",
            site_type="saas_corporate_marketing",
        )
        db.add(crawl)
        db.flush()
        db.add(
            Website(
                website_url=crawl.website_url,
                domain="example.com",
                crawl_id=crawl.id,
                category=crawl.site_type,
                site_type="normal",
                discovery_source="tavily",
                discovery_provider="tavily",
            )
        )
        db.commit()
        website_id = db.scalar(select(Website.id))

    response = client.get("/api/v1/websites")

    assert response.status_code == 200
    website = response.json()[0]
    assert website["id"] == str(website_id)
    assert website["website_url"] == "https://example.com/"
    assert website["domain"] == "example.com"
    assert website["status"] == "new"
    assert "company_id" not in website
    assert "review_status" not in website
    assert website["crawl_id"] == str(crawl.id)
    assert website["is_crawled"] is True
    assert website["crawl_job_status"] == "succeeded"
    assert website["category"] == "saas_corporate_marketing"
    assert website["site_type"] == "normal"
    assert website["discovery_source"] == "tavily"
    assert website["discovery_provider"] == "tavily"
    assert website["created_at"]
    assert website["crawled_at"]


def test_website_detail_includes_crawl_content_signals_and_job_history(client):
    with TestingSessionLocal() as db:
        crawl = WebsiteCrawl(
            website_url="https://detail.example/",
            site_type="ecommerce_retail",
            home_page_url="https://detail.example/",
            home_content="Welcome to the store.",
            about_page_url="https://detail.example/about",
            about_content="About the store.",
            emails=["hello@detail.example"],
            social_links=["https://social.example/detail"],
            classification_signals=[
                {
                    "url": "https://detail.example/",
                    "rendering_mode": "chromium",
                    "classification_signals": {
                        "page_title": "Detail Store",
                        "json_ld_scripts": ['{"@type":"Store"}'],
                        "cta_texts": ["Shop now"],
                    },
                },
            ],
        )
        db.add(crawl)
        db.flush()
        website = Website(
            website_url=crawl.website_url,
            domain="detail.example",
            crawl_id=crawl.id,
            category=crawl.site_type,
            page_type="ecommerce",
            site_topic=["home goods"],
            language="en",
            site_type="normal",
            discovery_source="tavily",
            discovery_provider="tavily",
        )
        db.add(website)
        db.add_all(
            [
                WebsiteCrawlJob(
                    website_url=website.website_url,
                    status="failed",
                    attempts=2,
                    error="Temporary failure",
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ),
                WebsiteCrawlJob(
                    website_url=website.website_url,
                    status="succeeded",
                    attempts=1,
                    crawl_id=crawl.id,
                    created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                ),
            ]
        )
        db.commit()
        website_id = website.id

    response = client.get(f"/api/v1/websites/{website_id}")

    assert response.status_code == 200
    result = response.json()
    assert result["website"]["domain"] == "detail.example"
    assert result["website"]["status"] == "new"
    assert result["website"]["crawl_job_status"] == "succeeded"
    assert result["crawl"]["home_content"] == "Welcome to the store."
    assert result["crawl"]["about_content"] == "About the store."
    assert result["crawl"]["emails"] == ["hello@detail.example"]
    assert result["crawl"]["social_links"] == [
        "https://social.example/detail",
    ]
    assert (
        result["crawl"]["classification_signals"][0]
        ["classification_signals"]["json_ld_scripts"]
        == ['{"@type":"Store"}']
    )
    assert [job["status"] for job in result["crawl_jobs"]] == [
        "succeeded",
        "failed",
    ]
    assert result["crawl_jobs"][1]["error"] == "Temporary failure"


def test_website_detail_returns_404_for_unknown_id(client):
    response = client.get(f"/api/v1/websites/{uuid4()}")

    assert response.status_code == 404


def test_company_domain_is_added_to_website_registry(client):
    response = client.post(
        "/api/v1/companies",
        json={"name": "Example Company", "domain": "example.com"},
    )

    assert response.status_code == 201
    with TestingSessionLocal() as db:
        website = db.scalar(select(Website))

    assert website is not None
    assert website.website_url == "https://example.com/"
    assert website.domain == "example.com"
    assert website.status == "new"
    assert website.discovery_source == "company_profile"


def test_listing_discovery_is_unlinked_and_not_queued_for_pagespeed(
    client,
    monkeypatch,
):
    result = DiscoveryResult(
        title="Acme Directory",
        url="https://directory.example/companies/acme",
        content="Business directory listing",
        score=0.9,
        provider=DiscoveryProvider.OCTOPARSE,
    )

    async def fake_search(query, db, max_results=5):
        return DiscoverySearchResponse(query=query, results=[result])

    async def fake_crawl(website_url, **_options):
        return CrawlData(
            website_url=website_url,
            site_type="directories_listings",
            about_page_url=None,
            about_content=None,
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
        )

    async def fake_enqueue(_job_ids):
        return None

    monkeypatch.setattr(lead_ingestion, "search_companies", fake_search)
    monkeypatch.setattr(lead_ingestion, "crawl_website", fake_crawl)
    monkeypatch.setattr(lead_ingestion, "enqueue_pagespeed_jobs", fake_enqueue)

    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "acme", "max_results": 5},
    )

    assert response.status_code == 200
    assert response.json()["pagespeed_scans_queued"] == 0
    with TestingSessionLocal() as db:
        website = db.scalar(select(Website))
        company = db.scalar(select(Company))
        crawl = db.scalar(select(WebsiteCrawl))
        pagespeed_job = db.scalar(select(PageSpeedScanJob))

    assert website is not None
    assert website.website_url == "https://directory.example/"
    assert website.domain == "directory.example"
    assert website.site_type == "listing"
    assert website.category == "directories_listings"
    assert website.discovery_source == "listing_website"
    assert website.discovery_provider == "octoparse"
    assert website.crawl_id == crawl.id
    assert company is not None
    assert company.website is None
    assert company.domain is None
    assert pagespeed_job is None
    listing_response = client.get("/api/v1/websites?site_type=listing")
    assert listing_response.status_code == 200
    assert len(listing_response.json()) == 1
