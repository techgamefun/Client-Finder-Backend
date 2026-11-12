import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from fastapi.testclient import TestClient

from conftest import TestingSessionLocal
from app import worker as website_crawl_worker
from app.models import Company, LeadSource, WebsiteCrawl, WebsiteCrawlJob
from app.services import website_crawler as app_website_crawler
from app.services import redis_queue
from app.services.redis_queue import RedisQueueError
from crawler_service import crawler as website_crawler
import main as crawler_service_api


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
        "company_links": [],
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
        app_website_crawler.crawl_website("https://example.com")
    )

    assert len(received) == 1
    assert str(received[0].url) == "http://crawler.internal:8001/crawl"
    assert json.loads(received[0].content) == {
        "website_url": "https://example.com"
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
            result = "job-123" if json[0] == "RPOP" else 1
            return httpx.Response(
                200,
                json={"result": result},
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
        return await redis_queue.dequeue_job()

    assert asyncio.run(exercise_queue()) == "job-123"
    assert received_commands == [
        ["LPUSH", "website-crawl:jobs", "job-123"],
        ["RPOP", "website-crawl:jobs"],
    ]


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


def test_worker_uses_database_recovery_when_redis_is_unavailable(
    monkeypatch,
):
    recovered_queues: list[str] = []

    def fail_redis_validation() -> None:
        raise RedisQueueError("Redis credentials are not configured.")

    def no_crawl_job():
        recovered_queues.append("crawl")
        return None

    def no_pagespeed_job():
        recovered_queues.append("pagespeed")
        return None

    async def stop_after_recovery(_seconds: float) -> None:
        raise RuntimeError("stop worker test")

    monkeypatch.setattr(
        website_crawl_worker,
        "validate_redis_configuration",
        fail_redis_validation,
    )
    monkeypatch.setattr(
        website_crawl_worker,
        "_find_recoverable_job",
        no_crawl_job,
    )
    monkeypatch.setattr(
        website_crawl_worker,
        "_find_recoverable_pagespeed_job",
        no_pagespeed_job,
    )
    monkeypatch.setattr(
        website_crawl_worker.asyncio,
        "sleep",
        stop_after_recovery,
    )

    with pytest.raises(RuntimeError, match="stop worker test"):
        asyncio.run(website_crawl_worker.run_worker())

    assert set(recovered_queues) == {"crawl", "pagespeed"}


def test_standalone_crawler_service_exposes_crawl_endpoint(monkeypatch):
    async def fake_crawl_website(website_url: str):
        return website_crawler.CrawlData(
            website_url=website_url.rstrip("/") + "/",
            about_page_url=None,
            about_content="Company overview.",
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
            emails=[],
            social_links=[],
        )

    monkeypatch.setattr(
        crawler_service_api,
        "crawl_website",
        fake_crawl_website,
    )

    with TestClient(crawler_service_api.app) as client:
        response = client.post(
            "/crawl",
            json={"website_url": "https://example.com"},
        )

    assert response.status_code == 200
    assert response.json()["website_url"] == "https://example.com/"
    assert response.json()["about_content"] == "Company overview."


class MockBrowserResponse:
    def __init__(self, response: httpx.Response) -> None:
        self.status = response.status_code
        self.status_text = response.reason_phrase
        self.headers = response.headers


class MockBrowserRoute:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(
            url=url,
            resource_type="document",
            is_navigation_request=lambda: True,
            redirected_from=None,
        )
        self.aborted = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        return None


class MockBrowserPage:
    def __init__(self, context) -> None:
        self.context = context
        self.url = ""
        self.response: httpx.Response | None = None

    async def goto(self, url: str, **_kwargs) -> MockBrowserResponse:
        route = MockBrowserRoute(url)
        await self.context.route_handler(route)
        if route.aborted:
            raise website_crawler.PlaywrightError("Request aborted")
        self.response = self.context.handle_request(httpx.Request("GET", url))
        self.url = url
        return MockBrowserResponse(self.response)

    async def content(self) -> str:
        assert self.response is not None
        return self.response.text

    async def close(self) -> None:
        return None


class MockBrowserContext:
    def __init__(self, handle_request) -> None:
        self.handle_request = handle_request
        self.route_handler = None

    async def route(self, _pattern: str, handler) -> None:
        self.route_handler = handler

    async def unroute(self, _pattern: str, _handler) -> None:
        self.route_handler = None

    async def new_page(self) -> MockBrowserPage:
        return MockBrowserPage(self)

    async def close(self) -> None:
        return None


def mock_browser(monkeypatch, handle_request) -> None:
    context = MockBrowserContext(handle_request)

    async def resolved(value):
        return value

    browser = SimpleNamespace(
        new_context=lambda **_kwargs: resolved(context),
        close=lambda: asyncio.sleep(0),
    )
    chromium = SimpleNamespace(
        launch=lambda: resolved(browser),
    )
    playwright = SimpleNamespace(chromium=chromium)

    class Manager:
        async def __aenter__(self):
            return playwright

        async def __aexit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(website_crawler, "async_playwright", Manager)


def test_normalize_website_url_preserves_source_path():
    assert website_crawler._normalize_website_url(
        "https://example.com/company/about?campaign=search#contact"
    ) == "https://example.com/company/about"


def test_crawl_website_extracts_pages_emails_and_social_links(monkeypatch):
    pages = {
        "/": """
            <a href="/about-us">About us</a>
            <a href="/contact">Contact</a>
            <a href="/mission">Our mission</a>
            <a href="mailto:hello@example.com">Email us</a>
            <a href="tel:+1-555-0100">Call us</a>
            <a href="https://www.linkedin.com/company/example">LinkedIn</a>
        """,
        "/about-us": "<p>Example company overview.</p>",
        "/contact": "<p>Contact: support@example.com</p>",
        "/mission": "<p>Our mission is to help local businesses grow.</p>",
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=f"<html><body>{body}</body></html>",
        )

    async def allow_public_url(_: str) -> None:
        return None

    mock_browser(monkeypatch, handle_request)
    monkeypatch.setattr(website_crawler, "_validate_public_url", allow_public_url)

    result = asyncio.run(website_crawler.crawl_website("https://example.com"))

    assert result.website_url == "https://example.com/"
    assert result.about_page_url == "https://example.com/about-us"
    assert result.about_content == "Example company overview."
    assert result.contact_content == "Contact: support@example.com"
    assert result.goals_page_url == "https://example.com/mission"
    assert result.goals_content == "Our mission is to help local businesses grow."
    assert result.emails == ["hello@example.com", "support@example.com"]
    assert result.phones == ["+15550100"]
    assert result.social_links == ["https://www.linkedin.com/company/example"]


def test_crawler_rejects_local_websites():
    with pytest.raises(HTTPException) as error:
        website_crawler._assert_public_host("127.0.0.1")

    assert error.value.status_code == 400


def test_retry_delay_is_bounded_by_retry_after():
    assert website_crawler._retry_delay("60", attempt=0) is None


def test_crawler_reports_failed_page_url_and_http_status(monkeypatch):
    request_count = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(503, text="Unavailable")

    async def allow_public_url(_: str) -> None:
        return None

    async def skip_retry_delay(_: float) -> None:
        return None

    mock_browser(monkeypatch, handle_request)
    monkeypatch.setattr(website_crawler, "_validate_public_url", allow_public_url)
    monkeypatch.setattr(website_crawler.asyncio, "sleep", skip_retry_delay)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            website_crawler.crawl_website(
                "https://example.com/company?private-token=hidden"
            )
        )

    assert error.value.status_code == 502
    assert "https://example.com/company" in error.value.detail
    assert "HTTP 503 Service Unavailable" in error.value.detail
    assert "private-token" not in error.value.detail
    assert request_count == website_crawler.MAX_RETRIES + 1


def test_crawler_explains_403_without_retrying_or_bypassing(monkeypatch):
    request_count = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(403, text="Forbidden")

    async def allow_public_url(_: str) -> None:
        return None

    mock_browser(monkeypatch, handle_request)
    monkeypatch.setattr(website_crawler, "_validate_public_url", allow_public_url)

    with pytest.raises(HTTPException) as error:
        asyncio.run(website_crawler.crawl_website("https://example.com"))

    assert error.value.status_code == 502
    assert "HTTP 403 Forbidden" in error.value.detail
    assert "authorized" in error.value.detail
    assert request_count == 1


def test_crawler_retries_temporary_server_failure(monkeypatch):
    request_count = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(503, text="Unavailable")
        body = (
            '<a href="/about">About</a>'
            if request.url.path == "/"
            else "<p>Recovered page</p>"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=body,
        )

    async def allow_public_url(_: str) -> None:
        return None

    async def skip_retry_delay(_: float) -> None:
        return None

    mock_browser(monkeypatch, handle_request)
    monkeypatch.setattr(website_crawler, "_validate_public_url", allow_public_url)
    monkeypatch.setattr(website_crawler.asyncio, "sleep", skip_retry_delay)

    result = asyncio.run(website_crawler.crawl_website("https://example.com"))

    assert request_count == 3
    assert result.about_content == "Recovered page"
    assert result.website_url == "https://example.com/"


def test_browser_crawler_extracts_javascript_rendered_content(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = (
                "<main id='content'></main>"
                "<script>document.querySelector('#content').textContent="
                "'Rendered by Chromium'</script>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def allow_public_url(_: str) -> None:
        return None

    monkeypatch.setattr(website_crawler, "_validate_public_url", allow_public_url)

    async def crawl_local_page():
        async with website_crawler.async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                context = await browser.new_context(service_workers="block")
                try:
                    return await website_crawler._fetch_page(
                        context,
                        f"http://127.0.0.1:{server.server_port}/",
                        "127.0.0.1",
                    )
                finally:
                    await context.close()
            finally:
                await browser.close()

    try:
        page = asyncio.run(crawl_local_page())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert page is not None
    assert page.content == "Rendered by Chromium"


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
    assert [queued_job["id"] for queued_job in queue_response.json()] == [
        job["id"]
    ]

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WebsiteCrawl)) == 0
        assert db.scalar(select(func.count()).select_from(Company)) == 0
        assert db.scalar(select(func.count()).select_from(LeadSource)) == 0
        assert db.get(WebsiteCrawlJob, UUID(job["id"])) is not None


def test_worker_persists_results_and_updates_job_state(client, monkeypatch):
    async def fake_enqueue(_job_id: str) -> None:
        return None

    async def fake_crawl_website(_website_url: str):
        return app_website_crawler.CrawlData(
            website_url="https://example.com/",
            home_page_url="https://example.com/",
            home_content="Welcome to Example.",
            about_page_url="https://example.com/about",
            about_content="About the company.",
            blog_page_url="https://example.com/blog",
            blog_content="Company updates.",
            service_page_url="https://example.com/services",
            service_content="Our services.",
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
            emails=["support@example.com"],
            social_links=["https://www.linkedin.com/company/example"],
        )

    monkeypatch.setattr(
        "app.services.crawl_jobs.enqueue_job",
        fake_enqueue,
    )
    monkeypatch.setattr(website_crawl_worker, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(
        website_crawl_worker,
        "crawl_website",
        fake_crawl_website,
    )

    queued = client.post(
        "/api/v1/crawler/crawl",
        json={"website_url": "https://example.com"},
    ).json()
    asyncio.run(website_crawl_worker.process_job(UUID(queued["id"])))

    completed = client.get(f"/api/v1/crawler/jobs/{queued['id']}")
    assert completed.status_code == 200
    result = completed.json()
    assert result["status"] == "succeeded"
    assert result["attempts"] == 1
    assert result["started_at"] is not None
    assert result["completed_at"] is not None
    assert result["result"]["website_url"] == "https://example.com/"
    assert result["result"]["home_page_url"] == "https://example.com/"
    assert result["result"]["home_content"] == "Welcome to Example."
    assert result["result"]["about_content"] == "About the company."
    assert result["result"]["blog_content"] == "Company updates."
    assert result["result"]["service_content"] == "Our services."
    assert result["result"]["emails"] == ["support@example.com"]
    assert result["result"]["social_links"] == [
        "https://www.linkedin.com/company/example"
    ]

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WebsiteCrawl)) == 1


def test_worker_records_crawl_failure(client, monkeypatch):
    async def fake_enqueue(_job_id: str) -> None:
        return None

    async def fail_crawl(_website_url: str):
        raise HTTPException(
            status_code=502,
            detail="The crawler service failed.",
        )

    monkeypatch.setattr(
        "app.services.crawl_jobs.enqueue_job",
        fake_enqueue,
    )
    monkeypatch.setattr(website_crawl_worker, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(website_crawl_worker, "crawl_website", fail_crawl)

    queued = client.post(
        "/api/v1/crawler/crawl",
        json={"website_url": "https://example.com"},
    ).json()
    asyncio.run(website_crawl_worker.process_job(UUID(queued["id"])))

    failed = client.get(f"/api/v1/crawler/jobs/{queued['id']}")
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["error"] == "The crawler service failed."


def test_worker_recovers_queued_job_without_redis_delivery(client, monkeypatch):
    monkeypatch.setattr(website_crawl_worker, "SessionLocal", TestingSessionLocal)
    job = WebsiteCrawlJob(
        website_url="https://example.com/",
        status="queued",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=60),
    )
    with TestingSessionLocal() as db:
        db.add(job)
        db.commit()
        job_id = job.id

    assert website_crawl_worker._find_recoverable_job() == job_id


def test_worker_requeues_stale_running_job(client, monkeypatch):
    monkeypatch.setattr(website_crawl_worker, "SessionLocal", TestingSessionLocal)
    job = WebsiteCrawlJob(
        website_url="https://example.com/",
        status="running",
        attempts=1,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    with TestingSessionLocal() as db:
        db.add(job)
        db.commit()
        job_id = job.id

    assert website_crawl_worker._find_recoverable_job() == job_id
    with TestingSessionLocal() as db:
        recovered = db.get(WebsiteCrawlJob, job_id)
        assert recovered is not None
        assert recovered.status == "queued"
        assert recovered.started_at is None
        assert recovered.error == "Retrying after the worker stopped unexpectedly."


def test_crawl_jobs_update_existing_site_instead_of_duplicating(
    client,
    monkeypatch,
):
    call_count = 0

    async def fake_enqueue(_job_id: str) -> None:
        return None

    async def fake_crawl_website(_website_url: str):
        nonlocal call_count
        call_count += 1
        return app_website_crawler.CrawlData(
            website_url=_website_url.rstrip("/") + "/",
            about_page_url=None,
            about_content=f"Crawl {call_count}",
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
            emails=[],
            social_links=[],
        )

    monkeypatch.setattr(
        "app.services.crawl_jobs.enqueue_job",
        fake_enqueue,
    )
    monkeypatch.setattr(
        website_crawl_worker,
        "SessionLocal",
        TestingSessionLocal,
    )
    monkeypatch.setattr(
        website_crawl_worker,
        "crawl_website",
        fake_crawl_website,
    )

    first = client.post(
        "/api/v1/crawler/crawl",
        json={"website_url": "https://example.com"},
    )
    second = client.post(
        "/api/v1/crawler/crawl",
        json={"website_url": "https://example.com"},
    )

    assert first.status_code == second.status_code == 202
    asyncio.run(website_crawl_worker.process_job(UUID(first.json()["id"])))
    asyncio.run(website_crawl_worker.process_job(UUID(second.json()["id"])))

    first_result = client.get(
        f"/api/v1/crawler/jobs/{first.json()['id']}"
    ).json()
    second_result = client.get(
        f"/api/v1/crawler/jobs/{second.json()['id']}"
    ).json()
    assert first_result["result"]["id"] == second_result["result"]["id"]
    assert second_result["result"]["about_content"] == "Crawl 2"

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WebsiteCrawl)) == 1


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
