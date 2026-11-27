from datetime import datetime, timedelta, timezone

from conftest import TestingSessionLocal
from app.models import (
    PageSpeedScan,
    SearchKeyword,
    Website,
    WebsiteCorpusAnalysis,
    WebsiteCrawl,
    WebsiteHypeStatProfile,
)


def test_website_analytics_aggregates_all_sites_and_latest_pagespeed(client):
    now = datetime.now(timezone.utc)
    with TestingSessionLocal() as db:
        crawl = WebsiteCrawl(
            website_url="https://crawled.example/",
            site_type="saas_corporate_marketing",
            crawled_at=now,
        )
        db.add(crawl)
        db.flush()
        uncrawled = Website(
            website_url="https://uncrawled.example/",
            domain="uncrawled.example",
            status="new",
            category=None,
            page_type=None,
            discovery_source="manual",
            ahrefs_domain_rating=30,
        )
        crawled = Website(
            website_url=crawl.website_url,
            domain="crawled.example",
            crawl_id=crawl.id,
            status="qualified",
            category="saas_corporate_marketing",
            page_type="normal",
            discovery_source="tavily",
            discovery_provider="tavily",
            ahrefs_domain_rating=70,
        )
        db.add_all([uncrawled, crawled])
        db.flush()
        discovery_keyword = SearchKeyword(
            keyword="SaaS marketing",
            searched_at=now,
        )
        repeated_discovery_keyword = SearchKeyword(
            keyword=" saas   marketing ",
            searched_at=now - timedelta(days=1),
        )
        crawled.search_keywords.extend(
            [discovery_keyword, repeated_discovery_keyword],
        )
        uncrawled.search_keywords.append(discovery_keyword)
        db.add_all(
            [
                WebsiteCorpusAnalysis(
                    website_id=crawled.id,
                    words=[
                        {
                            "word": "SaaS",
                            "score": 1.0,
                            "is_phrase": False,
                        },
                        {
                            "word": "strategy",
                            "score": 1.0,
                            "is_phrase": False,
                        },
                        {
                            "word": "business intelligence",
                            "score": 2.0,
                            "is_phrase": True,
                        },
                    ],
                    algorithm_version=2,
                    source_crawled_at=crawl.crawled_at,
                    computed_at=now,
                ),
                PageSpeedScan(
                    website_id=crawled.id,
                    website_url=crawled.website_url,
                    strategy="desktop",
                    category_scores={
                        "performance": 40,
                        "accessibility": 60,
                        "best-practices": 70,
                        "seo": 80,
                    },
                    scanned_at=now - timedelta(days=1),
                ),
                PageSpeedScan(
                    website_id=crawled.id,
                    website_url=crawled.website_url,
                    strategy="desktop",
                    category_scores={
                        "performance": 92,
                        "accessibility": 88,
                        "best-practices": 94,
                        "seo": 100,
                    },
                    scanned_at=now,
                ),
                PageSpeedScan(
                    website_id=crawled.id,
                    website_url=crawled.website_url,
                    strategy="mobile",
                    category_scores={
                        "performance": 48,
                        "accessibility": 90,
                        "best-practices": 80,
                        "seo": 100,
                    },
                    scanned_at=now,
                ),
                WebsiteHypeStatProfile(
                    website_id=crawled.id,
                    status="succeeded",
                    payload={"data": {}},
                ),
            ]
        )
        db.commit()

    response = client.get("/api/v1/websites/analytics")

    assert response.status_code == 200
    data = response.json()
    assert data["total_websites"] == 2
    assert data["crawled_websites"] == 1
    assert data["crawled_percentage"] == 50.0
    assert data["coverage"] == {
        "domain_rating": 2,
        "hypestat": 1,
        "pagespeed_any": 1,
        "pagespeed_desktop": 1,
        "pagespeed_mobile": 1,
    }
    assert {item["key"]: item["count"] for item in data["status_breakdown"]} == {
        "qualified": 1,
        "new": 1,
    }
    assert data["domain_rating"]["average"] == 50.0
    assert data["domain_rating"]["median"] == 50.0
    assert data["pagespeed_desktop"]["averages"]["performance"] == 92.0
    assert data["pagespeed_desktop"]["performance_distribution"] == [
        {"key": "poor", "count": 0, "percentage": 0.0},
        {"key": "needs_improvement", "count": 0, "percentage": 0.0},
        {"key": "good", "count": 1, "percentage": 100.0},
    ]
    assert data["pagespeed_mobile"]["averages"]["performance"] == 48.0
    assert data["content_keywords"] == {
        "analyzed_websites": 1,
        "crawled_websites": 1,
        "unique_words": 2,
        "unique_phrases": 1,
        "top_words": [
            {
                "keyword": "saas",
                "website_count": 1,
                "website_percentage": 100.0,
            },
            {
                "keyword": "strategy",
                "website_count": 1,
                "website_percentage": 100.0,
            },
        ],
        "top_phrases": [
            {
                "keyword": "business intelligence",
                "website_count": 1,
                "website_percentage": 100.0,
            },
        ],
    }
    assert data["discovery_keywords"] == {
        "unique_queries": 1,
        "linked_websites": 2,
        "top_queries": [
            {
                "keyword": "saas marketing",
                "website_count": 2,
                "website_percentage": 100.0,
                "search_count": 2,
            },
        ],
    }


def test_website_analytics_excludes_corpus_terms_from_older_crawl(client):
    now = datetime.now(timezone.utc)
    with TestingSessionLocal() as db:
        crawl = WebsiteCrawl(
            website_url="https://stale.example/",
            site_type="saas_corporate_marketing",
            crawled_at=now,
        )
        db.add(crawl)
        db.flush()
        website = Website(
            website_url=crawl.website_url,
            domain="stale.example",
            crawl_id=crawl.id,
            status="qualified",
            category="saas_corporate_marketing",
            page_type="normal",
            discovery_source="tavily",
        )
        db.add(website)
        db.flush()
        db.add(
            WebsiteCorpusAnalysis(
                website_id=website.id,
                words=[{"word": "stale", "score": 1.0, "is_phrase": False}],
                algorithm_version=2,
                source_crawled_at=now - timedelta(days=1),
                computed_at=now,
            ),
        )
        db.commit()

    response = client.get("/api/v1/websites/analytics")

    assert response.status_code == 200
    content_keywords = response.json()["content_keywords"]
    assert content_keywords["analyzed_websites"] == 0
    assert content_keywords["crawled_websites"] == 1
    assert content_keywords["unique_words"] == 0
    assert content_keywords["top_words"] == []
