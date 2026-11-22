from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from conftest import TestingSessionLocal
from app.models import (
    Company,
    ExcludedDomain,
    PageSpeedScan,
    PageSpeedScanJob,
    SearchKeyword,
    Website,
    WebsiteCrawl,
    WebsiteCrawlJob,
    WebsiteHypeStatProfile,
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
    assert "site_type" not in website
    assert website["discovery_source"] == "tavily"
    assert website["discovery_provider"] == "tavily"
    assert website["created_at"]
    assert website["crawled_at"]
    website_parameters = client.get("/openapi.json").json()["paths"][
        "/api/v1/websites"
    ]["get"]["parameters"]
    assert "site_type" not in {
        parameter["name"] for parameter in website_parameters
    }


def test_websites_endpoint_returns_hypestat_table_summaries(client):
    with TestingSessionLocal() as db:
        website = Website(
            website_url="https://metrics.example/",
            domain="metrics.example",
        )
        db.add(website)
        db.flush()
        db.add(
            WebsiteHypeStatProfile(
                website_id=website.id,
                status="succeeded",
                payload={
                    "entry": {
                        "domain": "metrics.example",
                        "status": "ok",
                        "data": {
                            "traffic": {
                                "visits_monthly": 2314375,
                                "countries": [
                                    {"country": "India", "visits": 80},
                                    {"name": "United States", "visits": 20},
                                ],
                            },
                            "rank": {
                                "hypestat": 43457,
                                "country": {
                                    "country": "India",
                                    "country_code": "IN",
                                },
                            },
                            "backlinks": {
                                "referring_domains": 403,
                                "by_country": [
                                    {"country": "Singapore", "domains": 191},
                                    {"country": "United States", "domains": 132},
                                ],
                            },
                        },
                    },
                    "meta": {"count": 1},
                },
            )
        )
        db.commit()
        website_id = website.id

    response = client.get("/api/v1/websites")

    assert response.status_code == 200
    result = next(
        row for row in response.json() if row["id"] == str(website_id)
    )
    assert result["hypestat_monthly_visits"] == 2314375
    assert result["hypestat_referring_domains"] == 403
    assert result["hypestat_global_rank"] == 43457
    assert result["hypestat_country"] == "India"
    assert result["hypestat_referring_domains_by_country"] == [
        {"country": "Singapore", "domains": 191},
        {"country": "United States", "domains": 132},
    ]
    assert result["hypestat_audience_countries"] == [
        "India",
        "United States",
    ]


def test_website_registry_page_filters_and_paginates_on_the_backend(client):
    with TestingSessionLocal() as db:
        for domain, status, topics in [
            ("alpha.example", "lead", ["saas"]),
            ("bravo.example", "new", ["commerce"]),
            ("charlie.example", "checked", ["saas"]),
        ]:
            crawl = WebsiteCrawl(
                website_url=f"https://{domain}/",
                site_type="saas_corporate_marketing",
                classification_signals=(
                    [
                        {
                            "url": f"https://{domain}/",
                            "classification_signals": {
                                "page_title": domain,
                                "heading_texts": ["Business"],
                            },
                        }
                    ]
                    if domain == "alpha.example"
                    else []
                ),
            )
            db.add(crawl)
            db.flush()
            website = Website(
                website_url=crawl.website_url,
                domain=domain,
                crawl_id=crawl.id,
                site_topic=topics,
                status=status,
                page_type="business_site",
                discovery_source="tavily",
            )
            db.add(website)
            db.flush()
            if domain == "alpha.example":
                db.add(
                    WebsiteHypeStatProfile(
                        website_id=website.id,
                        status="succeeded",
                        payload={
                            "entry": {
                                "data": {
                                    "traffic": {
                                        "visits_monthly": 1200,
                                        "countries": [{"country": "India"}],
                                    },
                                    "rank": {
                                        "hypestat": 300,
                                        "country": {"country": "India"},
                                    },
                                    "backlinks": {
                                        "referring_domains": 25,
                                        "by_country": [
                                            {"country": "US", "domains": 10}
                                        ],
                                    },
                                }
                            }
                        },
                    )
                )
        db.add(
            Website(
                website_url="https://unindexed.example/",
                domain="unindexed.example",
                site_topic=[],
            )
        )
        db.commit()

    first_page = client.get(
        "/api/v1/websites/registry-page",
        params={
            "limit": 1,
            "offset": 0,
            "sort_by": "website",
            "sort_direction": "ascending",
        },
    )
    second_page = client.get(
        "/api/v1/websites/registry-page",
        params={
            "limit": 1,
            "offset": 1,
            "sort_by": "website",
            "sort_direction": "ascending",
        },
    )
    beyond_last_page = client.get(
        "/api/v1/websites/registry-page",
        params={
            "limit": 1,
            "offset": 99,
            "sort_by": "website",
            "sort_direction": "ascending",
        },
    )

    assert first_page.status_code == 200
    assert first_page.json()["total_count"] == 3
    assert first_page.json()["indexed_count"] == 3
    assert first_page.json()["website_count"] == 4
    assert [row["domain"] for row in first_page.json()["items"]] == [
        "alpha.example"
    ]
    assert [row["domain"] for row in second_page.json()["items"]] == [
        "bravo.example"
    ]
    assert [row["domain"] for row in beyond_last_page.json()["items"]] == [
        "charlie.example"
    ]
    assert first_page.json()["items"][0]["crawl_signals"] == []

    filter_cases = [
        ({"status": "lead"}, 1),
        ({"category": "saas"}, 2),
        ({"discovery_source": "tavily"}, 3),
        ({"minimum_monthly_visits": 1000}, 1),
        ({"minimum_referring_domains": 20}, 1),
        ({"maximum_global_rank": 500}, 1),
        ({"hypestat_country": "India"}, 1),
        ({"backlink_country": "US"}, 1),
        ({"audience_country": "India"}, 1),
        ({"search": "alpha"}, 1),
    ]
    for filters, total_count in filter_cases:
        filter_response = client.get(
            "/api/v1/websites/registry-page",
            params=filters,
        )
        assert filter_response.status_code == 200
        assert filter_response.json()["total_count"] == total_count, filters

    filtered_page = client.get(
        "/api/v1/websites/registry-page",
        params={
            "status": "lead",
            "category": "saas",
            "discovery_source": "tavily",
            "minimum_monthly_visits": 1000,
            "minimum_referring_domains": 20,
            "maximum_global_rank": 500,
            "hypestat_country": "India",
            "backlink_country": "US",
            "audience_country": "India",
            "search": "alpha",
        },
    )

    assert filtered_page.status_code == 200
    assert filtered_page.json()["total_count"] == 1
    assert [row["domain"] for row in filtered_page.json()["items"]] == [
        "alpha.example"
    ]

    facets = client.get("/api/v1/websites/registry-facets")
    assert facets.status_code == 200
    assert facets.json()["categories"] == ["commerce", "saas"]
    assert facets.json()["discovery_sources"] == ["tavily"]
    assert facets.json()["hypestat_countries"] == ["India"]
    assert facets.json()["backlink_countries"] == ["US"]
    assert facets.json()["audience_countries"] == ["India"]


def test_website_registry_hides_excluded_domains_without_deleting_them(client):
    with TestingSessionLocal() as db:
        for domain, topic in [
            ("excluded.example", "hidden-root"),
            ("shop.excluded.example", "hidden-subdomain"),
            ("visible.example", "visible"),
        ]:
            crawl = WebsiteCrawl(
                website_url=f"https://{domain}/",
                site_type="saas_corporate_marketing",
            )
            db.add(crawl)
            db.flush()
            db.add(
                Website(
                    website_url=crawl.website_url,
                    domain=domain,
                    crawl_id=crawl.id,
                    site_topic=[topic],
                    discovery_source=topic,
                )
            )
        exclusion = ExcludedDomain(
            domain="excluded.example",
            reason="Manual Exclusion",
            source="user",
        )
        db.add(exclusion)
        db.commit()
        exclusion_id = exclusion.id

    response = client.get(
        "/api/v1/websites/registry-page",
        params={"limit": 1, "offset": 0, "sort_by": "website"},
    )
    facets = client.get("/api/v1/websites/registry-facets")

    assert response.status_code == 200
    assert response.json()["total_count"] == 1
    assert response.json()["indexed_count"] == 1
    assert response.json()["website_count"] == 1
    assert [item["domain"] for item in response.json()["items"]] == [
        "visible.example"
    ]
    assert facets.status_code == 200
    assert facets.json()["categories"] == ["visible"]
    assert facets.json()["discovery_sources"] == ["visible"]

    with TestingSessionLocal() as db:
        assert db.scalar(
            select(Website).where(Website.domain == "excluded.example")
        ) is not None
        db.delete(db.get(ExcludedDomain, exclusion_id))
        db.commit()

    restored = client.get("/api/v1/websites/registry-page")
    assert restored.status_code == 200
    assert restored.json()["total_count"] == 3
    assert {item["domain"] for item in restored.json()["items"]} == {
        "excluded.example",
        "shop.excluded.example",
        "visible.example",
    }


def test_job_queue_summaries_return_only_running_and_failed_counts(client):
    with TestingSessionLocal() as db:
        website = Website(
            website_url="https://queue.example/",
            domain="queue.example",
        )
        db.add(website)
        db.flush()
        db.add_all(
            [
                WebsiteCrawlJob(
                    website_url=website.website_url,
                    status="running",
                ),
                WebsiteCrawlJob(
                    website_url=website.website_url,
                    status="failed",
                ),
                WebsiteCrawlJob(
                    website_url=website.website_url,
                    status="succeeded",
                ),
                PageSpeedScanJob(
                    website_id=website.id,
                    website_url=website.website_url,
                    strategy="mobile",
                    status="running",
                ),
                PageSpeedScanJob(
                    website_id=website.id,
                    website_url=website.website_url,
                    strategy="desktop",
                    status="failed",
                ),
                PageSpeedScanJob(
                    website_id=website.id,
                    website_url=website.website_url,
                    strategy="mobile",
                    status="succeeded",
                ),
            ]
        )
        db.commit()

    crawler_summary = client.get("/api/v1/crawler/jobs/summary")
    pagespeed_summary = client.get("/api/v1/pagespeed/jobs/summary")

    assert crawler_summary.status_code == 200
    assert crawler_summary.json() == {
        "running_count": 1,
        "failed_count": 1,
    }
    assert pagespeed_summary.status_code == 200
    assert pagespeed_summary.json() == {
        "running_count": 1,
        "failed_count": 1,
    }


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
            discovery_source="tavily",
            discovery_provider="tavily",
        )
        db.add(website)
        website.search_keywords.append(
            SearchKeyword(
                keyword="home goods manufacturers",
                searched_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            )
        )
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
    assert len(result["discovery_keywords"]) == 1
    assert result["discovery_keywords"][0]["keyword"] == (
        "home goods manufacturers"
    )
    assert result["discovery_keywords"][0]["searched_at"].startswith(
        "2026-01-03T00:00:00"
    )


def test_website_detail_returns_404_for_unknown_id(client):
    response = client.get(f"/api/v1/websites/{uuid4()}")

    assert response.status_code == 404


def test_website_detail_includes_pagespeed_summaries_and_report(client):
    with TestingSessionLocal() as db:
        website = Website(
            website_url="https://speed.example/",
            domain="speed.example",
        )
        other_website = Website(
            website_url="https://other-speed.example/",
            domain="other-speed.example",
        )
        db.add(website)
        db.add(other_website)
        db.flush()
        scan = PageSpeedScan(
            website_id=website.id,
            website_url=website.website_url,
            strategy="mobile",
            category_scores={
                "performance": 87,
                "accessibility": 96,
                "best-practices": 100,
                "seo": 91,
            },
            report={
                "lighthouseResult": {
                    "lighthouseVersion": "12.0.0",
                    "audits": {
                        "first-contentful-paint": {
                            "title": "First Contentful Paint",
                            "score": 0.9,
                            "displayValue": "1.2 s",
                        },
                    },
                },
            },
            scanned_at=datetime(2026, 1, 4, tzinfo=timezone.utc),
        )
        db.add(scan)
        db.commit()
        website_id = website.id
        other_website_id = other_website.id
        scan_id = scan.id

    response = client.get(f"/api/v1/websites/{website_id}")

    assert response.status_code == 200
    summaries = response.json()["pagespeed_scans"]
    assert len(summaries) == 1
    assert summaries[0]["id"] == str(scan_id)
    assert summaries[0]["strategy"] == "mobile"
    assert summaries[0]["category_scores"]["performance"] == 87

    report_response = client.get(
        f"/api/v1/websites/{website_id}/pagespeed-scans/{scan_id}"
    )
    assert report_response.status_code == 200
    assert (
        report_response.json()["report"]["lighthouseResult"]["audits"][
            "first-contentful-paint"
        ]["displayValue"]
        == "1.2 s"
    )

    unrelated_scan_response = client.get(
        f"/api/v1/websites/{other_website_id}/pagespeed-scans/{scan_id}"
    )
    assert unrelated_scan_response.status_code == 404


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
    assert website.page_type == "business_listing"
    assert website.category == "directories_listings"
    assert website.discovery_source == "listing_website"
    assert website.discovery_provider == "octoparse"
    assert website.crawl_id == crawl.id
    assert company is not None
    assert company.website is None
    assert company.domain is None
    assert pagespeed_job is None
    listing_response = client.get(
        "/api/v1/websites?page_type=business_listing"
    )
    assert listing_response.status_code == 200
    assert len(listing_response.json()) == 1
    assert "site_type" not in listing_response.json()[0]
