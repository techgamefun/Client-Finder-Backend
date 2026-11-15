import json

import httpx
from sqlalchemy import select

from conftest import TestingSessionLocal
from app.models import Website, WebsiteCrawl, WebsiteCrawlJob
from app.services import website_classification


def test_reclassify_website_uses_saved_homepage_without_creating_crawl(
    client,
    monkeypatch,
):
    with TestingSessionLocal() as db:
        crawl = WebsiteCrawl(
            website_url="https://saved.example/",
            site_type="saas_corporate_marketing",
            home_page_url="https://saved.example/",
            home_content="Property management software for rental homes.",
            classification_signals=[
                {
                    "url": "https://saved.example/about",
                    "classification_signals": {"page_title": "About us"},
                },
                {
                    "url": "https://saved.example/",
                    "classification_signals": {
                        "page_title": "Saved homepage",
                        "heading_texts": ["Homepage heading"],
                    },
                },
            ],
        )
        db.add(crawl)
        db.flush()
        website = Website(
            website_url=crawl.website_url,
            domain="saved.example",
            crawl_id=crawl.id,
            category=crawl.site_type,
            page_type="other",
            site_topic=["old topic"],
            classification_manual=True,
            discovery_source="tavily",
        )
        db.add(website)
        db.commit()
        website_id = website.id
        crawl_id = crawl.id
        crawled_at = crawl.crawled_at

    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "site_type": "directories_listings",
                "website_topics": ["property management"],
            },
        )

    real_async_client = httpx.AsyncClient

    def mock_async_client(**kwargs):
        return real_async_client(
            transport=httpx.MockTransport(handle_request),
            **kwargs,
        )

    monkeypatch.setattr(
        website_classification.httpx,
        "AsyncClient",
        mock_async_client,
    )

    response = client.post(
        f"/api/v1/websites/{website_id}/reclassify",
    )

    assert response.status_code == 200
    assert response.json()["page_type"] == "business_listing"
    assert response.json()["site_topic"] == ["property management"]
    assert len(requests) == 1
    assert requests[0].url.path == "/classify-saved"
    request_payload = json.loads(requests[0].content)
    assert request_payload["website_url"] == "https://saved.example/"
    assert request_payload["body_text"] == (
        "Property management software for rental homes."
    )
    assert request_payload["signals"]["page_title"] == "Saved homepage"

    with TestingSessionLocal() as db:
        updated_website = db.get(Website, website_id)
        unchanged_crawl = db.get(WebsiteCrawl, crawl_id)
        crawl_jobs = db.scalars(select(WebsiteCrawlJob)).all()

    assert updated_website is not None
    assert updated_website.page_type == "business_listing"
    assert updated_website.site_topic == ["property management"]
    assert updated_website.classification_manual is False
    assert unchanged_crawl is not None
    assert unchanged_crawl.crawled_at == crawled_at
    assert unchanged_crawl.home_content == (
        "Property management software for rental homes."
    )
    assert crawl_jobs == []


def test_reclassify_website_requires_saved_crawl(client):
    with TestingSessionLocal() as db:
        website = Website(
            website_url="https://uncrawled.example/",
            domain="uncrawled.example",
            discovery_source="manual",
        )
        db.add(website)
        db.commit()
        website_id = website.id

    response = client.post(
        f"/api/v1/websites/{website_id}/reclassify",
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "This website has no saved crawl data to classify."
    )


def test_bulk_reclassifies_unclassified_or_empty_topic_websites(
    client,
    monkeypatch,
):
    with TestingSessionLocal() as db:
        missing_type_crawl = WebsiteCrawl(
            website_url="https://missing-type.example/",
            home_page_url="https://missing-type.example/",
            home_content="Saved homepage for the missing type.",
            classification_signals=[
                {
                    "url": "https://missing-type.example/",
                    "classification_signals": {"page_title": "Missing type"},
                },
            ],
        )
        missing_topic_crawl = WebsiteCrawl(
            website_url="https://missing-topic.example/",
            home_page_url="https://missing-topic.example/",
            home_content="Saved homepage for the missing topic.",
            classification_signals=[
                {
                    "url": "https://missing-topic.example/",
                    "classification_signals": {"page_title": "Missing topic"},
                },
            ],
        )
        complete_crawl = WebsiteCrawl(
            website_url="https://complete.example/",
            home_page_url="https://complete.example/",
            classification_signals=[
                {
                    "url": "https://complete.example/",
                    "classification_signals": {"page_title": "Complete"},
                },
            ],
        )
        missing_signals_crawl = WebsiteCrawl(
            website_url="https://missing-signals.example/",
            home_page_url="https://missing-signals.example/",
        )
        db.add_all(
            [
                missing_type_crawl,
                missing_topic_crawl,
                complete_crawl,
                missing_signals_crawl,
            ]
        )
        db.flush()
        missing_type = Website(
            website_url=missing_type_crawl.website_url,
            domain="missing-type.example",
            crawl_id=missing_type_crawl.id,
            page_type=None,
            site_topic=["existing topic"],
            classification_manual=True,
            discovery_source="manual",
        )
        missing_topic = Website(
            website_url=missing_topic_crawl.website_url,
            domain="missing-topic.example",
            crawl_id=missing_topic_crawl.id,
            page_type="business_site",
            site_topic=[],
            classification_manual=True,
            discovery_source="manual",
        )
        complete = Website(
            website_url=complete_crawl.website_url,
            domain="complete.example",
            crawl_id=complete_crawl.id,
            page_type="news",
            site_topic=["existing topic"],
            discovery_source="manual",
        )
        missing_signals = Website(
            website_url=missing_signals_crawl.website_url,
            domain="missing-signals.example",
            crawl_id=missing_signals_crawl.id,
            page_type=None,
            site_topic=[],
            discovery_source="manual",
        )
        uncrawled = Website(
            website_url="https://uncrawled.example/",
            domain="uncrawled.example",
            page_type=None,
            site_topic=[],
            discovery_source="manual",
        )
        db.add_all([missing_type, missing_topic, complete, missing_signals, uncrawled])
        db.commit()
        missing_type_id = missing_type.id
        missing_topic_id = missing_topic.id
        complete_id = complete.id
        missing_signals_id = missing_signals.id

    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "websites": [
                    {
                        "website_id": site["website_id"],
                        "site_type": (
                            "directories_listings"
                            if site["website_id"] == str(missing_type_id)
                            else "saas_corporate_marketing"
                        ),
                        "website_topics": [
                            "recomputed",
                            site["website_id"],
                        ],
                    }
                    for site in payload["sites"]
                ],
            },
        )

    real_async_client = httpx.AsyncClient

    def mock_async_client(**kwargs):
        return real_async_client(
            transport=httpx.MockTransport(handle_request),
            **kwargs,
        )

    monkeypatch.setattr(
        website_classification.httpx,
        "AsyncClient",
        mock_async_client,
    )

    response = client.post(
        "/api/v1/websites/reclassify-unclassified",
    )

    assert response.status_code == 200
    assert response.json() == {
        "eligible_count": 3,
        "recomputed_count": 2,
        "skipped_count": 1,
    }
    assert len(requests) == 1
    assert requests[0].url.path == "/classify-saved-batch"
    requested_ids = {
        site["website_id"]
        for site in json.loads(requests[0].content)["sites"]
    }
    assert requested_ids == {str(missing_type_id), str(missing_topic_id)}

    with TestingSessionLocal() as db:
        updated_missing_type = db.get(Website, missing_type_id)
        updated_missing_topic = db.get(Website, missing_topic_id)
        unchanged_complete = db.get(Website, complete_id)
        unchanged_missing_signals = db.get(Website, missing_signals_id)
        jobs = db.scalars(select(WebsiteCrawlJob)).all()

    assert updated_missing_type is not None
    assert updated_missing_type.page_type == "business_listing"
    assert updated_missing_type.site_topic == [
        "recomputed",
        str(missing_type_id),
    ]
    assert updated_missing_type.classification_manual is False
    assert updated_missing_topic is not None
    assert updated_missing_topic.page_type == "business_site"
    assert updated_missing_topic.site_topic == [
        "recomputed",
        str(missing_topic_id),
    ]
    assert updated_missing_topic.classification_manual is False
    assert unchanged_complete is not None
    assert unchanged_complete.page_type == "news"
    assert unchanged_complete.site_topic == ["existing topic"]
    assert unchanged_missing_signals is not None
    assert unchanged_missing_signals.page_type is None
    assert unchanged_missing_signals.site_topic == []
    assert jobs == []


def test_bulk_reclassification_returns_zero_when_no_crawled_sites_are_eligible(
    client,
):
    response = client.post(
        "/api/v1/websites/reclassify-unclassified",
    )

    assert response.status_code == 200
    assert response.json() == {
        "eligible_count": 0,
        "recomputed_count": 0,
        "skipped_count": 0,
    }


def test_classification_batches_respect_crawler_item_limit():
    payloads = [{"website_id": str(index)} for index in range(26)]

    batches = website_classification._classification_payload_batches(payloads)

    assert [len(batch) for batch in batches] == [25, 1]
