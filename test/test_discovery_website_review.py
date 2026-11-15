from sqlalchemy import func, select

from conftest import TestingSessionLocal
from app.models import Website, WebsiteCrawlJob, WebsiteHypeStatProfile
from app.services import crawl_jobs
from app.services.hypestat import HypeStatFetchResult


def _disable_redis_enqueue(monkeypatch):
    enqueued: list[str] = []

    async def fake_enqueue(job_id: str) -> None:
        enqueued.append(job_id)

    monkeypatch.setattr(crawl_jobs, "enqueue_job", fake_enqueue)
    return enqueued


def test_saving_discovery_results_canonicalizes_domains_and_queues_crawls(
    client,
    monkeypatch,
):
    enqueued = _disable_redis_enqueue(monkeypatch)
    response = client.post(
        "/api/v1/discovery/save-websites",
        json={
            "query": "local manufacturing news",
            "results": [
                {"url": "http://www.example.com/news/story", "provider": "tavily"},
                {"url": "https://example.com/contact", "provider": "octoparse"},
            ],
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["websites_saved"] == 1
    assert result["crawl_jobs_enqueued"] == 1
    assert result["crawl_jobs_already_present"] == 0
    assert result["domains_skipped"] == 1
    assert len(enqueued) == 1

    with TestingSessionLocal() as db:
        websites = db.scalars(select(Website)).all()
        jobs = db.scalars(select(WebsiteCrawlJob)).all()
        website_keywords = [
            keyword.keyword for keyword in websites[0].search_keywords
        ]

    assert len(websites) == 1
    assert websites[0].website_url == "https://example.com/"
    assert websites[0].domain == "example.com"
    assert websites[0].status == "new"
    assert websites[0].site_topic == []
    assert websites[0].crawl_id is None
    assert website_keywords == ["local manufacturing news"]
    assert jobs[0].website_url == "https://example.com/"
    assert jobs[0].status == "queued"

    website_response = client.get("/api/v1/websites")
    assert website_response.status_code == 200
    assert website_response.json()[0]["crawl_job_status"] == "queued"


def test_repeat_discovery_reuses_domain_and_does_not_queue_another_job(
    client,
    monkeypatch,
):
    enqueued = _disable_redis_enqueue(monkeypatch)
    first = client.post(
        "/api/v1/discovery/save-websites",
        json={
            "query": "manufacturing companies",
            "results": [
                {"url": "https://www.example.com/about", "provider": "tavily"},
            ],
        },
    )
    second = client.post(
        "/api/v1/discovery/save-websites",
        json={
            "query": "manufacturing companies",
            "results": [
                {"url": "http://example.com/products", "provider": "octoparse"},
            ],
        },
    )

    assert first.status_code == second.status_code == 200
    assert second.json()["websites_saved"] == 1
    assert second.json()["crawl_jobs_enqueued"] == 0
    assert second.json()["crawl_jobs_already_present"] == 1
    assert len(enqueued) == 1

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Website)) == 1
        assert db.scalar(select(func.count()).select_from(WebsiteCrawlJob)) == 1
        website = db.scalar(select(Website))

    assert website is not None
    assert website.status == "new"
    assert website.discovery_provider == "tavily"


def test_saving_discovery_websites_ignores_snippet_fields(client, monkeypatch):
    _disable_redis_enqueue(monkeypatch)
    response = client.post(
        "/api/v1/discovery/save-websites",
        json={
            "query": "local manufacturing news",
            "results": [
                {
                    "url": "https://example.com/news",
                    "provider": "tavily",
                    "title": "Result title",
                    "content": "Snippet text",
                },
            ],
        },
    )

    assert response.status_code == 200
    with TestingSessionLocal() as db:
        website = db.scalar(select(Website))
        assert website is not None
        assert website.website_url == "https://example.com/"
        assert "title" not in Website.__table__.columns
        assert "snippet" not in Website.__table__.columns


def test_saving_websites_persists_hypestat_profiles_without_losing_websites(
    client,
    monkeypatch,
):
    _disable_redis_enqueue(monkeypatch)

    async def fake_fetch_profiles(domains, _api_key):
        assert domains == ["known.example", "unavailable.example"]
        return {
            "known.example": HypeStatFetchResult(
                status="succeeded",
                payload={
                    "entry": {
                        "domain": "known.example",
                        "status": "ok",
                        "data": {
                            "traffic": {"visits_daily": 1234},
                            "whois": {"registrar": "Example Registrar"},
                        },
                    },
                    "meta": {"requests_left_today": 999},
                },
            ),
            "unavailable.example": HypeStatFetchResult(
                status="failed",
                error="HypeStat returned HTTP 429: Daily limit reached.",
            ),
        }

    monkeypatch.setattr(
        "app.services.discovery_websites.fetch_hypestat_profiles",
        fake_fetch_profiles,
    )
    response = client.post(
        "/api/v1/discovery/save-websites",
        json={
            "query": "industrial suppliers",
            "results": [
                {"url": "https://known.example/about", "provider": "tavily"},
                {
                    "url": "https://unavailable.example/",
                    "provider": "tavily",
                },
            ],
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["websites_saved"] == 2
    assert result["hypestat_profiles_requested"] == 2
    assert result["hypestat_profiles_succeeded"] == 1
    assert result["hypestat_profiles_failed"] == 1

    with TestingSessionLocal() as db:
        websites = db.scalars(select(Website).order_by(Website.domain)).all()
        profiles = db.scalars(
            select(WebsiteHypeStatProfile).order_by(
                WebsiteHypeStatProfile.website_id
            )
        ).all()
        successful_website_id = next(
            website.id
            for website in websites
            if website.domain == "known.example"
        )

    assert len(profiles) == 2
    successful = next(
        profile
        for profile in profiles
        if profile.payload is not None
    )
    failed = next(profile for profile in profiles if profile.error is not None)
    assert successful.status == "succeeded"
    assert successful.payload["entry"]["data"]["traffic"]["visits_daily"] == 1234
    assert successful.fetched_at is not None
    assert failed.status == "failed"
    assert failed.error == "HypeStat returned HTTP 429: Daily limit reached."
    assert failed.fetched_at is None

    detail = client.get(f"/api/v1/websites/{successful_website_id}")
    assert detail.status_code == 200
    assert (
        detail.json()["hypestat_profile"]["payload"]["entry"]["data"]["whois"]
        ["registrar"]
        == "Example Registrar"
    )


def test_saving_websites_can_skip_automatic_hypestat_lookups(
    client,
    monkeypatch,
):
    _disable_redis_enqueue(monkeypatch)
    settings_response = client.put(
        "/api/v1/settings",
        json={"hypestat_auto_fetch_enabled": False},
    )
    assert settings_response.status_code == 200

    async def unexpected_hypestat_fetch(*_args):
        raise AssertionError("HypeStat should be disabled")

    monkeypatch.setattr(
        "app.services.discovery_websites.fetch_hypestat_profiles",
        unexpected_hypestat_fetch,
    )
    response = client.post(
        "/api/v1/discovery/save-websites",
        json={
            "query": "industrial suppliers",
            "results": [
                {"url": "https://disabled.example/", "provider": "tavily"},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["hypestat_profiles_requested"] == 0
    with TestingSessionLocal() as db:
        website = db.scalar(select(Website))
        assert website is not None
        assert db.get(WebsiteHypeStatProfile, website.id) is None


def test_website_status_stays_new_when_classification_is_updated(
    client,
    monkeypatch,
):
    _disable_redis_enqueue(monkeypatch)
    response = client.post(
        "/api/v1/discovery/save-websites",
        json={
            "query": "local furniture makers",
            "results": [
                {"url": "https://maker.example/about", "provider": "tavily"},
            ],
        },
    )
    assert response.status_code == 200

    with TestingSessionLocal() as db:
        website = db.scalar(select(Website))
        assert website is not None
        website_id = website.id

    updated = client.patch(
        f"/api/v1/websites/{website_id}",
        json={
            "page_type": "business_site",
            "site_topic": ["furniture", "manufacturing", "furniture"],
            "language": "en",
        },
    )

    assert updated.status_code == 200
    assert updated.json()["status"] == "new"
    assert updated.json()["crawl_job_status"] == "queued"
    assert updated.json()["site_topic"] == ["furniture", "manufacturing"]
    assert "company_id" not in updated.json()
    assert "review_status" not in updated.json()
    status_update = client.patch(
        f"/api/v1/websites/{website_id}",
        json={"status": "crawled"},
    )
    assert status_update.status_code == 422
