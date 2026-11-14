from sqlalchemy import func, select

from conftest import TestingSessionLocal
from app.models import Website, WebsiteCrawlJob
from app.services import crawl_jobs


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

    assert len(websites) == 1
    assert websites[0].website_url == "https://example.com/"
    assert websites[0].domain == "example.com"
    assert websites[0].status == "new"
    assert websites[0].site_topic == []
    assert websites[0].crawl_id is None
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
