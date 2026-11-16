import asyncio
from uuid import UUID

import httpx

from app.schemas.discovery import (
    DiscoveryResult,
    DiscoverySearchResponse,
    TavilyDiscoverySearchRequest,
)
from app.api import discovery
from app.services import discovery as discovery_service
from app.models import SearchKeyword
from conftest import TestingSessionLocal


def test_frontend_origin_is_allowed_for_api_requests(client):
    response = client.options(
        "/api/v1/companies",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert (
        response.headers["access-control-allow-origin"]
        == "http://localhost:3000"
    )
    assert "authorization" in response.headers[
        "access-control-allow-headers"
    ].casefold()


def test_search_endpoint_returns_validated_results(client, monkeypatch):
    search_response = DiscoverySearchResponse(
        query="manufacturing companies",
        results=[
            DiscoveryResult(
                title="Example Manufacturing",
                url="https://example.com",
                content="Manufacturing company",
                score=0.9,
            )
        ],
    )
    received: dict[str, object] = {}

    async def fake_search(query: str, max_results: int, db, *, options):
        received["query"] = query
        received["max_results"] = max_results
        received["options"] = options
        return search_response.results, [], [], None, None

    monkeypatch.setattr(discovery, "search_with_selected_tools", fake_search)

    response = client.post(
        "/api/v1/discovery/search",
        json={"query": "manufacturing companies", "max_results": 20},
    )

    assert response.status_code == 200
    data = response.json()
    assert data == {
        "query": "manufacturing companies",
        "results": [
            {
                "title": "Example Manufacturing",
                "url": "https://example.com/",
                "content": "Manufacturing company",
                "score": 0.9,
                "provider": "tavily",
                "favicon": None,
                "published_date": None,
            }
        ],
        "search_id": data["search_id"],
        "answer": None,
        "credits_used": None,
        "provider_jobs": [],
        "provider_failures": [],
    }
    UUID(data["search_id"])
    assert received == {
        "query": "manufacturing companies",
        "max_results": 20,
        "options": TavilyDiscoverySearchRequest(
            query="manufacturing companies",
            max_results=20,
        ),
    }

    with TestingSessionLocal() as db:
        search = db.get(SearchKeyword, UUID(data["search_id"]))
        assert search is not None
        assert search.keyword == "manufacturing companies"
        assert search.searched_at is not None

    over_limit = client.post(
        "/api/v1/discovery/search",
        json={"query": "manufacturing companies", "max_results": 21},
    )
    assert over_limit.status_code == 422

    suggestions = client.get(
        "/api/v1/discovery/suggestions",
        params={"query": "FACTURING"},
    )
    assert suggestions.status_code == 200
    assert suggestions.json()[0]["id"] == data["search_id"]
    assert suggestions.json()[0]["keyword"] == "manufacturing companies"


def test_search_rejects_conflicting_or_invalid_filter_values(client):
    invalid_requests = [
        {
            "query": "manufacturing companies",
            "time_range": "week",
            "start_date": "2026-01-01",
        },
        {
            "query": "manufacturing companies",
            "start_date": "2026-02-01",
            "end_date": "2026-01-01",
        },
        {
            "query": "manufacturing companies",
            "search_depth": "deep",
        },
        {
            "query": "manufacturing companies",
            "country": "United States",
        },
        {
            "query": "manufacturing companies",
            "include_domains": ["https://example.com"],
        },
    ]

    for payload in invalid_requests:
        response = client.post("/api/v1/discovery/search", json=payload)
        assert response.status_code == 422


def test_tavily_payload_and_response_metadata_are_mapped(monkeypatch):
    captured_payload: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, timeout: float):
            assert timeout == 20.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, json: dict[str, object]):
            assert url == discovery_service.TAVILY_SEARCH_URL
            captured_payload.update(json)
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "answer": "Market summary",
                    "usage": {"credits": 2},
                    "results": [
                        {
                            "title": "Good result",
                            "url": "https://good.example",
                            "content": "Relevant",
                            "score": 0.9,
                            "favicon": "https://good.example/favicon.ico",
                            "published_date": "2026-11-15",
                        },
                        {
                            "title": "Low result",
                            "url": "https://low.example",
                            "content": "Not relevant enough",
                            "score": 0.2,
                        },
                    ],
                },
            )

    monkeypatch.setattr(
        discovery_service,
        "get_tavily_api_key",
        lambda _db: "tvly-test",
    )
    monkeypatch.setattr(discovery_service.httpx, "AsyncClient", FakeAsyncClient)
    options = TavilyDiscoverySearchRequest(
        query="manufacturing companies",
        max_results=7,
        search_depth="ultra-fast",
        topic="news",
        country="india",
        chunks_per_source=2,
        time_range="week",
        filter_by_published_date=True,
        include_domains=["*.example.com"],
        include_domains_mode="prefer",
        exclude_domains=["directory.example"],
        include_answer="advanced",
        include_favicon=True,
        auto_parameters=True,
        min_score=0.5,
    )

    response = asyncio.run(
        discovery_service.search_companies(
            options.query,
            db=None,
            options=options,
        )
    )

    assert captured_payload == {
        "api_key": "tvly-test",
        "query": "manufacturing companies",
        "search_depth": "ultra-fast",
        "topic": "news",
        "max_results": 7,
        "include_answer": "advanced",
        "include_favicon": True,
        "auto_parameters": True,
        "include_usage": True,
        "include_raw_content": False,
        "filter_by_published_date": True,
        "time_range": "week",
        "include_domains": ["*.example.com"],
        "include_domains_mode": "prefer",
        "exclude_domains": ["directory.example"],
    }
    assert len(response.results) == 1
    assert str(response.results[0].favicon) == "https://good.example/favicon.ico"
    assert response.results[0].published_date == "2026-11-15"
    assert response.answer == "Market summary"
    assert response.credits_used == 2


def test_tavily_depth_and_country_options_are_conditionally_sent(monkeypatch):
    captured_payload: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, timeout: float):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, json: dict[str, object]):
            captured_payload.update(json)
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"results": []},
            )

    monkeypatch.setattr(
        discovery_service,
        "get_tavily_api_key",
        lambda _db: "tvly-test",
    )
    monkeypatch.setattr(discovery_service.httpx, "AsyncClient", FakeAsyncClient)
    options = TavilyDiscoverySearchRequest(
        query="manufacturing companies",
        search_depth="fast",
        country="india",
    )

    asyncio.run(
        discovery_service.search_companies(
            options.query,
            db=None,
            options=options,
        )
    )

    assert captured_payload["chunks_per_source"] == 3
    assert captured_payload["country"] == "india"
