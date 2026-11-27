import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from conftest import TestingSessionLocal
from app.models import (
    WebsiteCrawl,
    WebsiteCrawlJob,
)
from app.services import website_crawler as app_website_crawler
from app.services import redis_queue
from app.services.redis_queue import RedisQueueError


def test_main_api_crawler_client_calls_standalone_service(monkeypatch):
    received: list[httpx.Request] = []
    service_response = {
        "website_url": "https://example.com/",
        "home_page_url": "https://example.com/",
        "home_content": "Welcome to Example.",
        "about_page_url": None,
        "about_content": "Company overview.",
        "blog_page_url": "https://example.com/blog",
        "blog_content": "Company updates.",
        "service_page_url": "https://example.com/services",
        "service_content": "Our services.",
        "contact_page_url": None,
        "contact_content": None,
        "goals_page_url": None,
        "goals_content": None,
        "emails": ["hello@example.com"],
        "social_links": [],
        "phones": [],
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json=service_response)

    real_async_client = httpx.AsyncClient

    def mock_async_client(**kwargs):
        return real_async_client(
            transport=httpx.MockTransport(handle_request),
            **kwargs,
        )

    monkeypatch.setattr(
        app_website_crawler.httpx,
        "AsyncClient",
        mock_async_client,
    )
    monkeypatch.setattr(
        app_website_crawler.settings,
        "crawler_service_url",
        "http://crawler.internal:8001/",
    )

    result = asyncio.run(
        app_website_crawler.crawl_website(
            "https://example.com",
            max_pages=4,
            max_depth=1,
        )
    )

    assert len(received) == 1
    assert str(received[0].url) == "http://crawler.internal:8001/crawl"
    assert json.loads(received[0].content) == {
        "website_url": "https://example.com",
        "max_pages": 4,
        "max_depth": 1,
    }
    assert result.about_content == "Company overview."
    assert result.home_content == "Welcome to Example."
    assert result.blog_page_url == "https://example.com/blog"
    assert result.service_content == "Our services."
    assert result.emails == ["hello@example.com"]


def test_main_api_crawler_client_preserves_service_errors(monkeypatch):
    real_async_client = httpx.AsyncClient

    def mock_async_client(**kwargs):
        return real_async_client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    400,
                    json={"detail": "A public website URL is required."},
                )
            ),
            **kwargs,
        )

    monkeypatch.setattr(
        app_website_crawler.httpx,
        "AsyncClient",
        mock_async_client,
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            app_website_crawler.crawl_website("http://localhost/private")
        )

    assert error.value.status_code == 400
    assert error.value.detail == "A public website URL is required."


def test_upstash_queue_uses_rest_commands(monkeypatch):
    received_commands: list[list[str]] = []

    class MockAsyncClient:
        def __init__(self, **_kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url, *, headers, json):
            assert url == "https://redis.example"
            assert headers == {"Authorization": "Bearer test-token"}
            received_commands.append(json)
            return httpx.Response(
                200,
                json={"result": 1},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        redis_queue.settings,
        "upstash_redis_rest_url",
        "https://redis.example/",
    )
    monkeypatch.setattr(
        redis_queue.settings,
        "upstash_redis_rest_token",
        "test-token",
    )
    monkeypatch.setattr(redis_queue.httpx, "AsyncClient", MockAsyncClient)

    async def exercise_queue():
        await redis_queue.enqueue_job("job-123")

    asyncio.run(exercise_queue())
    assert received_commands == [["LPUSH", "website-crawl:jobs", "job-123"]]


def test_upstash_queue_error_includes_provider_reason(monkeypatch):
    class MockAsyncClient:
        def __init__(self, **_kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url, *, headers, json):
            return httpx.Response(
                200,
                json={"error": "WRONGPASS invalid username-password pair"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        redis_queue.settings,
        "upstash_redis_rest_url",
        "https://redis.example/",
    )
    monkeypatch.setattr(
        redis_queue.settings,
        "upstash_redis_rest_token",
        "test-token",
    )
    monkeypatch.setattr(redis_queue.httpx, "AsyncClient", MockAsyncClient)

    with pytest.raises(RedisQueueError, match="WRONGPASS"):
        asyncio.run(redis_queue.enqueue_job("job-123"))


def test_crawl_endpoint_queues_job_and_exposes_status(client, monkeypatch):
    enqueued_ids: list[str] = []

    async def fake_enqueue(job_id: str) -> None:
        enqueued_ids.append(job_id)

    monkeypatch.setattr(
        "app.services.crawl_jobs.enqueue_job",
        fake_enqueue,
    )

    response = client.post(
        "/api/v1/crawler/crawl",
        json={"website_url": "https://example.com"},
    )

    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    assert job["website_url"] == "https://example.com/"
    assert job["started_at"] is None
    assert job["result"] is None
    assert enqueued_ids == [job["id"]]

    status_response = client.get(f"/api/v1/crawler/jobs/{job['id']}")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "queued"

    queue_response = client.get("/api/v1/crawler/jobs")
    assert queue_response.status_code == 200
    assert [queued_job["id"] for queued_job in queue_response.json()["items"]] == [
        job["id"]
    ]

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WebsiteCrawl)) == 0
        assert db.get(WebsiteCrawlJob, UUID(job["id"])) is not None


def test_crawl_endpoint_reports_redis_queue_failure(client, monkeypatch):
    async def fail_enqueue(_job_id: str) -> None:
        raise RedisQueueError("queue unavailable")

    monkeypatch.setattr(
        "app.services.crawl_jobs.enqueue_job",
        fail_enqueue,
    )

    response = client.post(
        "/api/v1/crawler/crawl",
        json={"website_url": "https://example.com"},
    )

    assert response.status_code == 202
    job_id = UUID(response.json()["id"])
    assert response.json()["status"] == "queued"
    assert response.json()["error"] is None
    assert response.json()["completed_at"] is None
    with TestingSessionLocal() as db:
        job = db.get(WebsiteCrawlJob, job_id)
        assert job is not None
        assert job.status == "queued"


def test_failed_crawl_job_can_be_retried(client, monkeypatch):
    enqueued_ids: list[str] = []

    async def fake_enqueue(job_id: str) -> None:
        enqueued_ids.append(job_id)

    monkeypatch.setattr(
        "app.services.crawl_jobs.enqueue_job",
        fake_enqueue,
    )
    with TestingSessionLocal() as db:
        job = WebsiteCrawlJob(
            website_url="https://example.com/",
            status="failed",
            attempts=3,
            error="The crawler service failed.",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            completed_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()
        job_id = job.id

    response = client.post(f"/api/v1/crawler/jobs/{job_id}/retry")

    assert response.status_code == 202
    retried = response.json()
    assert retried["status"] == "queued"
    assert retried["attempts"] == 0
    assert retried["error"] is None
    assert retried["started_at"] is None
    assert retried["completed_at"] is None
    assert enqueued_ids == [str(job_id)]


def test_non_failed_crawl_job_cannot_be_retried(client):
    with TestingSessionLocal() as db:
        job = WebsiteCrawlJob(
            website_url="https://example.com/",
            status="queued",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    response = client.post(f"/api/v1/crawler/jobs/{job_id}/retry")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Only failed website crawl jobs can be retried."
    )
