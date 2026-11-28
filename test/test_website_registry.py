from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from conftest import TestingSessionLocal
from app.models import (
    Contact,
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
from app.services import discovery_ingestion
from app.services.website_crawler import CrawlData


def test_websites_endpoint_returns_domain_and_crawl_details(client):
    with TestingSessionLocal() as db:
        crawl = WebsiteCrawl(
            website_url="https://example.com/",
            site_type="saas_corporate_marketing",
        )
        db.add(crawl)
        db.flush()
        website = Website(
            website_url=crawl.website_url,
            domain="example.com",
            crawl_id=crawl.id,
            category=crawl.site_type,
            discovery_source="tavily",
            discovery_provider="tavily",
        )
        db.add(website)
        db.flush()
        db.add_all(
            [
                Contact(
                    website_id=website.id,
                    business_email="hello@example.com",
                ),
                Contact(
                    website_id=website.id,
                    business_email="sales@example.com",
                ),
            ]
        )
        db.commit()
        website_id = website.id

    response = client.get("/api/v1/websites")

    assert response.status_code == 200
    website = response.json()[0]
    assert website["id"] == str(website_id)
    assert website["website_url"] == "https://example.com/"
    assert website["domain"] == "example.com"
    assert website["emails"] == ["hello@example.com", "sales@example.com"]
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
    registry_response = client.get("/api/v1/websites/registry-page")
    assert registry_response.status_code == 200
    assert registry_response.json()["items"][0]["emails"] == website["emails"]
    website_parameters = client.get("/openapi.json").json()["paths"][
        "/api/v1/websites"
    ]["get"]["parameters"]
    assert "site_type" not in {
        parameter["name"] for parameter in website_parameters
    }


def test_crawler_jobs_search_status_and_pagination(client):
    with TestingSessionLocal() as db:
        jobs = [
            WebsiteCrawlJob(
                website_url="https://alpha-one.example/",
                status="queued",
            ),
            WebsiteCrawlJob(
                website_url="https://alpha-two.example/",
                status="queued",
            ),
            WebsiteCrawlJob(
                website_url="https://alpha-three.example/",
                status="failed",
            ),
            WebsiteCrawlJob(
                website_url="https://beta.example/",
                status="queued",
            ),
        ]
        db.add_all(jobs)
        db.commit()
        expected_job_ids = {str(job.id) for job in jobs[:2]}

    response = client.get(
        "/api/v1/crawler/jobs",
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


def test_crawler_jobs_search_matches_failure_reason(client):
    with TestingSessionLocal() as db:
        matching_job = WebsiteCrawlJob(
            website_url="https://rendering.example/",
            status="failed",
            error="JavaScript rendering timed out",
        )
        other_failed_job = WebsiteCrawlJob(
            website_url="https://different-error.example/",
            status="failed",
            error="Connection refused",
        )
        db.add_all(
            [
                matching_job,
                other_failed_job,
                WebsiteCrawlJob(
                    website_url="https://queued.example/",
                    status="queued",
                ),
            ]
        )
        db.commit()
        expected_job_id = str(matching_job.id)

    response = client.get(
        "/api/v1/crawler/jobs",
        params={"search": "javascript rendering", "status": "failed"},
    )

    assert response.status_code == 200
    result = response.json()
    assert [job["id"] for job in result["items"]] == [expected_job_id]
    assert result["total_count"] == 1
    assert result["status_counts"] == {"failed": 1}


def test_crawler_job_failure_reason_options_and_filter(client):
    with TestingSessionLocal() as db:
        matching_jobs = [
            WebsiteCrawlJob(
                website_url=f"https://rendering-{index}.example/",
                status="failed",
                error=(
                    f"Crawler could not fetch https://rendering-{index}.example/: "
                    "JavaScript rendering timed out"
                ),
            )
            for index in range(2)
        ]
        db.add_all(
            [
                *matching_jobs,
                WebsiteCrawlJob(
                    website_url="https://blocked.example/",
                    status="failed",
                    error="Crawler could not fetch https://blocked.example/: Access denied",
                ),
                WebsiteCrawlJob(
                    website_url="https://queued.example/",
                    status="queued",
                    error="Should not be an option",
                ),
            ]
        )
        db.commit()
        expected_job_ids = {str(job.id) for job in matching_jobs}

    reasons_response = client.get("/api/v1/crawler/jobs/failure-reasons")
    assert reasons_response.status_code == 200
    assert reasons_response.json() == [
        "Access denied",
        "JavaScript rendering timed out",
    ]

    jobs_response = client.get(
        "/api/v1/crawler/jobs",
        params={
            "failure_reason": "JavaScript rendering timed out",
            "status": "failed",
        },
    )

    assert jobs_response.status_code == 200
    result = jobs_response.json()
    assert {job["id"] for job in result["items"]} == expected_job_ids
    assert result["total_count"] == 2
    assert result["status_counts"] == {"failed": 2}


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


def test_website_registry_page_includes_current_user_domain_rating(client):
    with TestingSessionLocal() as db:
        websites = []
        for domain in ("ahrefs-dr.example", "no-ahrefs-dr.example"):
            crawl = WebsiteCrawl(
                website_url=f"https://{domain}/",
                site_type="saas_corporate_marketing",
            )
            db.add(crawl)
            db.flush()
            website = Website(
                website_url=crawl.website_url,
                domain=domain,
                crawl_id=crawl.id,
            )
            if domain == "ahrefs-dr.example":
                website.ahrefs_domain_rating = 42.7
                website.ahrefs_domain_rating_license_url = (
                    "https://ahrefs.com/license"
                )
            db.add(website)
            db.flush()
            websites.append(website)
        db.commit()
        website_id = websites[0].id
        unrated_website_id = websites[1].id

    response = client.get("/api/v1/websites/registry-page")

    assert response.status_code == 200
    websites = {item["id"]: item for item in response.json()["items"]}
    assert websites[str(website_id)]["ahrefs_domain_rating"] == 42.7
    assert websites[str(unrated_website_id)]["ahrefs_domain_rating"] is None


def test_website_registry_sorts_by_first_email_case_insensitively(client):
    with TestingSessionLocal() as db:
        website_by_domain = {}
        for domain in (
            "zeta.example",
            "beta.example",
            "alpha.example",
            "no-email.example",
        ):
            website = Website(
                website_url=f"https://{domain}/",
                domain=domain,
            )
            db.add(website)
            db.flush()
            website_by_domain[domain] = website

        db.add_all(
            [
                Contact(
                    website_id=website_by_domain["zeta.example"].id,
                    business_email="zeta@example.com",
                ),
                Contact(
                    website_id=website_by_domain["beta.example"].id,
                    business_email="Zulu@example.com",
                ),
                Contact(
                    website_id=website_by_domain["beta.example"].id,
                    business_email="alpha@example.com",
                ),
                Contact(
                    website_id=website_by_domain["alpha.example"].id,
                    business_email="Bravo@example.com",
                ),
            ]
        )
        db.commit()

    ascending = client.get(
        "/api/v1/websites/registry-page",
        params={"sort_by": "emails", "sort_direction": "ascending"},
    )
    descending = client.get(
        "/api/v1/websites/registry-page",
        params={"sort_by": "emails", "sort_direction": "descending"},
    )

    assert ascending.status_code == 200
    assert [row["domain"] for row in ascending.json()["items"]] == [
        "beta.example",
        "alpha.example",
        "zeta.example",
        "no-email.example",
    ]
    assert descending.status_code == 200
    assert [row["domain"] for row in descending.json()["items"]] == [
        "zeta.example",
        "alpha.example",
        "beta.example",
        "no-email.example",
    ]


def test_website_registry_page_filters_and_paginates_on_the_backend(client):
    with TestingSessionLocal() as db:
        for domain, status, topics in [
            ("alpha.example", "qualified", ["saas"]),
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
                ahrefs_domain_rating={
                    "alpha.example": 72.5,
                    "bravo.example": 25.0,
                }.get(domain),
            )
            db.add(website)
            db.flush()
            if domain == "alpha.example":
                db.add_all(
                    [
                        PageSpeedScan(
                            website_id=website.id,
                            website_url=website.website_url,
                            strategy="desktop",
                            category_scores={
                                "performance": 25,
                                "accessibility": 60,
                                "best-practices": 65,
                                "seo": 70,
                            },
                            scanned_at=datetime(
                                2026,
                                10,
                                1,
                                tzinfo=timezone.utc,
                            ),
                        ),
                        PageSpeedScan(
                            website_id=website.id,
                            website_url=website.website_url,
                            strategy="desktop",
                            category_scores={
                                "performance": 75,
                                "accessibility": 89,
                                "best-practices": 77,
                                "seo": 100,
                            },
                            scanned_at=datetime(
                                2026,
                                11,
                                20,
                                tzinfo=timezone.utc,
                            ),
                        ),
                        PageSpeedScan(
                            website_id=website.id,
                            website_url=website.website_url,
                            strategy="mobile",
                            category_scores={
                                "performance": 59,
                                "accessibility": 95,
                                "best-practices": 73,
                                "seo": 100,
                            },
                            scanned_at=datetime(
                                2026,
                                11,
                                20,
                                tzinfo=timezone.utc,
                            ),
                        ),
                    ]
                )
            elif domain == "bravo.example":
                db.add_all(
                    [
                        PageSpeedScan(
                            website_id=website.id,
                            website_url=website.website_url,
                            strategy="desktop",
                            category_scores={
                                "performance": 60,
                                "accessibility": 80,
                                "best-practices": 80,
                                "seo": 85,
                            },
                            scanned_at=datetime(
                                2026,
                                11,
                                19,
                                tzinfo=timezone.utc,
                            ),
                        ),
                        PageSpeedScan(
                            website_id=website.id,
                            website_url=website.website_url,
                            strategy="mobile",
                            category_scores={
                                "performance": 40,
                                "accessibility": 90,
                                "best-practices": 70,
                                "seo": 90,
                            },
                            scanned_at=datetime(
                                2026,
                                11,
                                19,
                                tzinfo=timezone.utc,
                            ),
                        ),
                    ]
                )
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
    uncrawled_search = client.get(
        "/api/v1/websites/registry-page",
        params={"search": "unindexed.example"},
    )

    assert first_page.status_code == 200
    assert first_page.json()["total_count"] == 4
    assert first_page.json()["indexed_count"] == 3
    assert first_page.json()["website_count"] == 4
    assert [row["domain"] for row in first_page.json()["items"]] == [
        "alpha.example"
    ]
    assert [row["domain"] for row in second_page.json()["items"]] == [
        "bravo.example"
    ]
    assert [row["domain"] for row in beyond_last_page.json()["items"]] == [
        "unindexed.example"
    ]
    assert beyond_last_page.json()["items"][0]["is_crawled"] is False
    assert beyond_last_page.json()["items"][0]["crawl_id"] is None
    assert uncrawled_search.json()["total_count"] == 1
    assert uncrawled_search.json()["items"][0]["domain"] == "unindexed.example"
    assert first_page.json()["items"][0]["crawl_signals"] == []
    assert (
        first_page.json()["items"][0]["pagespeed_desktop"]["category_scores"][
            "performance"
        ]
        == 75
    )
    assert first_page.json()["items"][0]["pagespeed_desktop"][
        "scanned_at"
    ].startswith("2026-11-20")
    assert (
        first_page.json()["items"][0]["pagespeed_mobile"]["category_scores"][
            "seo"
        ]
        == 100
    )

    pagespeed_ascending = client.get(
        "/api/v1/websites/registry-page",
        params={
            "limit": 4,
            "sort_by": "pagespeed_desktop_performance",
            "sort_direction": "ascending",
        },
    )
    pagespeed_descending = client.get(
        "/api/v1/websites/registry-page",
        params={
            "limit": 4,
            "sort_by": "pagespeed_desktop_performance",
            "sort_direction": "descending",
        },
    )
    pagespeed_ascending_domains = [
        row["domain"] for row in pagespeed_ascending.json()["items"]
    ]
    pagespeed_descending_domains = [
        row["domain"] for row in pagespeed_descending.json()["items"]
    ]
    assert pagespeed_ascending_domains[:2] == [
        "bravo.example",
        "alpha.example",
    ]
    assert set(pagespeed_ascending_domains[2:]) == {
        "charlie.example",
        "unindexed.example",
    }
    assert pagespeed_descending_domains[:2] == [
        "alpha.example",
        "bravo.example",
    ]
    assert set(pagespeed_descending_domains[2:]) == {
        "charlie.example",
        "unindexed.example",
    }
    best_practices_sorted = client.get(
        "/api/v1/websites/registry-page",
        params={
            "limit": 4,
            "sort_by": "pagespeed_desktop_best_practices",
            "sort_direction": "descending",
        },
    )
    best_practices_domains = [
        row["domain"] for row in best_practices_sorted.json()["items"]
    ]
    assert best_practices_domains[:2] == ["bravo.example", "alpha.example"]
    assert set(best_practices_domains[2:]) == {
        "charlie.example",
        "unindexed.example",
    }

    dr_ascending = client.get(
        "/api/v1/websites/registry-page",
        params={
            "limit": 4,
            "sort_by": "ahrefs_domain_rating",
            "sort_direction": "ascending",
        },
    )
    dr_descending = client.get(
        "/api/v1/websites/registry-page",
        params={
            "limit": 4,
            "sort_by": "ahrefs_domain_rating",
            "sort_direction": "descending",
        },
    )
    dr_ascending_domains = [
        row["domain"] for row in dr_ascending.json()["items"]
    ]
    dr_descending_domains = [
        row["domain"] for row in dr_descending.json()["items"]
    ]
    assert dr_ascending_domains[:2] == ["bravo.example", "alpha.example"]
    assert set(dr_ascending_domains[2:]) == {
        "charlie.example",
        "unindexed.example",
    }
    assert dr_descending_domains[:2] == ["alpha.example", "bravo.example"]
    assert set(dr_descending_domains[2:]) == {
        "charlie.example",
        "unindexed.example",
    }

    filter_cases = [
        ({"status": "qualified"}, 1),
        ({"category": "saas"}, 2),
        ({"discovery_source": "tavily"}, 3),
        ({"minimum_monthly_visits": 1000}, 1),
        ({"minimum_referring_domains": 20}, 1),
        ({"maximum_global_rank": 500}, 1),
        ({"minimum_ahrefs_domain_rating": 50}, 1),
        ({"maximum_ahrefs_domain_rating": 30}, 1),
        (
            {
                "pagespeed_strategy": "desktop",
                "pagespeed_category": "performance",
                "minimum_pagespeed_score": 70,
            },
            1,
        ),
        (
            {
                "pagespeed_strategy": "mobile",
                "pagespeed_category": "seo",
                "minimum_pagespeed_score": 95,
            },
            1,
        ),
        (
            {
                "pagespeed_strategy": "mobile",
                "pagespeed_category": "best-practices",
                "minimum_pagespeed_score": 72,
            },
            1,
        ),
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
            "status": "qualified",
            "category": "saas",
            "discovery_source": "tavily",
            "minimum_monthly_visits": 1000,
            "minimum_referring_domains": 20,
            "maximum_global_rank": 500,
            "minimum_ahrefs_domain_rating": 70,
            "maximum_ahrefs_domain_rating": 80,
            "pagespeed_strategy": "desktop",
            "pagespeed_category": "performance",
            "minimum_pagespeed_score": 70,
            "maximum_pagespeed_score": 80,
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
        db.flush()
        db.add(
            Contact(
                website_id=website.id,
                business_email="hello@detail.example",
            )
        )
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
    assert "emails" not in result["crawl"]
    assert [contact["business_email"] for contact in result["contacts"]] == [
        "hello@detail.example"
    ]
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


def test_website_detail_includes_only_pagespeed_scores(client):
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

    scan_response = client.get(
        f"/api/v1/websites/{website_id}/pagespeed-scans/{scan_id}"
    )
    assert scan_response.status_code == 200
    assert "report" not in scan_response.json()
    assert set(scan_response.json()["category_scores"]) == {
        "performance",
        "accessibility",
        "best-practices",
        "seo",
    }

    unrelated_scan_response = client.get(
        f"/api/v1/websites/{other_website_id}/pagespeed-scans/{scan_id}"
    )
    assert unrelated_scan_response.status_code == 404


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

    monkeypatch.setattr(discovery_ingestion, "search_websites", fake_search)
    monkeypatch.setattr(discovery_ingestion, "crawl_website", fake_crawl)
    monkeypatch.setattr(discovery_ingestion, "enqueue_pagespeed_jobs", fake_enqueue)

    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "acme", "max_results": 5},
    )

    assert response.status_code == 200
    assert response.json()["pagespeed_scans_queued"] == 0
    with TestingSessionLocal() as db:
        website = db.scalar(select(Website))
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
    assert pagespeed_job is None
    listing_response = client.get(
        "/api/v1/websites?page_type=business_listing"
    )
    assert listing_response.status_code == 200
    assert len(listing_response.json()) == 1
    assert "site_type" not in listing_response.json()[0]
