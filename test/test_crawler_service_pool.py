import asyncio
from itertools import count

import httpx
import pytest
from fastapi import HTTPException

from app.config import settings
from app.services import website_crawler


@pytest.mark.parametrize(
    ("status_code", "expected_requests", "expected_delays"),
    [
        (400, 1, []),
        (503, 4, [15, 30, 60]),
    ],
)
def test_crawler_service_client_bounds_retries(
    monkeypatch,
    status_code,
    expected_requests,
    expected_delays,
):
    received: list[httpx.Request] = []
    delays: list[float] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(
            status_code,
            json={"detail": "Temporary service failure."},
        )

    async def skip_retry_delay(delay: float) -> None:
        delays.append(delay)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        website_crawler.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handle_request),
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        settings,
        "crawler_service_url",
        "http://crawler.internal:8001/",
    )
    monkeypatch.setattr(
        website_crawler.asyncio,
        "sleep",
        skip_retry_delay,
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(website_crawler.crawl_website("https://example.com"))

    assert error.value.status_code == status_code
    assert len(received) == expected_requests
    assert delays == expected_delays


def test_crawler_service_client_retries_transient_errors(monkeypatch):
    received: list[httpx.Request] = []
    delays: list[float] = []
    service_response = {
        "website_url": "https://example.com/",
        "home_page_url": None,
        "home_content": None,
        "about_page_url": None,
        "about_content": None,
        "contact_page_url": None,
        "contact_content": None,
        "goals_page_url": None,
        "goals_content": None,
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        received.append(request)
        if len(received) == 1:
            raise httpx.ConnectError("Service is starting.", request=request)
        if len(received) == 2:
            return httpx.Response(503, text="Service Unavailable")
        if len(received) == 3:
            return httpx.Response(504, text="Gateway Timeout")
        return httpx.Response(200, json=service_response)

    async def skip_retry_delay(delay: float) -> None:
        delays.append(delay)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        website_crawler.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handle_request),
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        settings,
        "crawler_service_url",
        "http://crawler.internal:8001/",
    )
    monkeypatch.setattr(
        website_crawler.asyncio,
        "sleep",
        skip_retry_delay,
    )

    result = asyncio.run(
        website_crawler.crawl_website("https://example.com")
    )

    assert result.website_url == "https://example.com/"
    assert len(received) == 4
    assert delays == [15, 30, 60]


def test_crawler_service_pool_round_robins_and_removes_health_path(monkeypatch):
    service_urls = [
        "https://webcrawler-i5cl.onrender.com",
        "https://webcrawler-3.onrender.com",
        "https://webcrawler-2.onrender.com",
        "https://webcrawler-1-3hus.onrender.com",
        "https://webcrawler-4.onrender.com",
    ]
    monkeypatch.setattr(
        settings,
        "crawler_service_urls",
        ",".join(f"{url}/health" for url in service_urls),
    )
    monkeypatch.setattr(website_crawler, "_crawler_service_index", count(0))

    assert [
        website_crawler._next_crawler_service_url()
        for _ in service_urls
    ] == service_urls


def test_crawler_service_pool_falls_back_to_single_url(monkeypatch):
    monkeypatch.setattr(settings, "crawler_service_urls", None)
    monkeypatch.setattr(
        settings,
        "crawler_service_url",
        "http://crawler.internal:8001/",
    )

    assert website_crawler._crawler_service_base_urls() == (
        "http://crawler.internal:8001",
    )
