import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from conftest import TestingSessionLocal
from app.models import Company, LeadSource, WebsiteCrawl
from app.services import website_crawler


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


def test_crawl_endpoint_persists_results_in_separate_table(client, monkeypatch):
    async def fake_crawl_website(_website_url: str):
        return website_crawler.CrawlData(
            website_url=_website_url.rstrip("/") + "/",
            about_page_url="https://example.com/about",
            about_content="About the company.",
            contact_page_url="https://example.com/contact",
            contact_content="Contact support@example.com.",
            goals_page_url=None,
            goals_content=None,
            emails=["support@example.com"],
            social_links=["https://www.linkedin.com/company/example"],
        )

    monkeypatch.setattr(
        website_crawler,
        "crawl_website",
        fake_crawl_website,
    )

    response = client.post(
        "/api/v1/crawler/crawl",
        json={"website_url": "https://example.com"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["website_url"] == "https://example.com/"
    assert result["about_content"] == "About the company."
    assert result["emails"] == ["support@example.com"]
    assert result["social_links"] == [
        "https://www.linkedin.com/company/example"
    ]

    with TestingSessionLocal() as db:
        crawls = db.scalars(select(WebsiteCrawl)).all()
        assert db.scalar(select(func.count()).select_from(Company)) == 0
        assert db.scalar(select(func.count()).select_from(LeadSource)) == 0
    assert len(crawls) == 1
    assert crawls[0].website_url == "https://example.com/"
    assert crawls[0].emails == ["support@example.com"]


def test_crawl_endpoint_updates_existing_site_instead_of_duplicating(
    client,
    monkeypatch,
):
    call_count = 0

    async def fake_crawl_website(_website_url: str):
        nonlocal call_count
        call_count += 1
        return website_crawler.CrawlData(
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
        website_crawler,
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

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["about_content"] == "Crawl 2"

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WebsiteCrawl)) == 1
