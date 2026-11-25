import asyncio
from uuid import UUID

import httpx

from app.schemas.discovery import (
    DiscoveryResult,
    DiscoverySearchResponse,
    DiscoveryProvider,
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
        "search_queries": ["manufacturing companies"],
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


def test_ai_search_expands_keywords_and_aggregates_tavily_results(
    client,
    monkeypatch,
):
    received: list[tuple[str, int, str]] = []

    monkeypatch.setattr(
        discovery,
        "get_user_provider_api_key",
        lambda _db, provider, *, user_id: (
            "ai-test-key" if provider == "groq" else None
        ),
    )
    monkeypatch.setattr(
        discovery,
        "get_tavily_api_key",
        lambda _db: "tavily-test-key",
    )

    async def fake_expand(keywords, provider, api_key, *, model):
        assert keywords == "IT companies in Denver"
        assert provider == "groq"
        assert api_key == "ai-test-key"
        assert model == "openai/gpt-oss-120b"
        return [
            keywords,
            "managed IT service providers Denver",
            "Denver technology consulting companies",
        ]

    async def fake_search(
        query,
        db,
        max_results,
        *,
        options,
        api_key,
        api_key_is_loaded,
    ):
        assert db is not None
        assert api_key == "tavily-test-key"
        assert api_key_is_loaded is True
        received.append((query, max_results, options.query))
        return DiscoverySearchResponse(
            query=query,
            results=[
                DiscoveryResult(
                    title=f"{query} company",
                    url=f"https://example{len(received)}.com",
                    content="Relevant result",
                    score=0.9,
                )
            ],
            answer=f"Summary for {query}",
            credits_used=1,
        )

    monkeypatch.setattr(
        discovery,
        "generate_discovery_search_queries",
        fake_expand,
    )
    monkeypatch.setattr(discovery, "search_companies", fake_search)

    response = client.post(
        "/api/v1/discovery/ai-search",
        json={
            "query": "IT companies in Denver",
            "max_results": 5,
            "ai_provider": "groq",
            "model": "openai/gpt-oss-120b",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "IT companies in Denver"
    assert data["search_queries"] == [
        "IT companies in Denver",
        "managed IT service providers Denver",
        "Denver technology consulting companies",
    ]
    assert len(data["results"]) == 3
    assert data["credits_used"] == 3
    assert len(data["search_id"]) == 36
    assert received == [
        ("IT companies in Denver", 2, "IT companies in Denver"),
        (
            "managed IT service providers Denver",
            2,
            "managed IT service providers Denver",
        ),
        (
            "Denver technology consulting companies",
            1,
            "Denver technology consulting companies",
        ),
    ]

    with TestingSessionLocal() as db:
        search = db.get(SearchKeyword, UUID(data["search_id"]))
        assert search is not None
        assert search.keyword == "IT companies in Denver"


def test_search_runs_multiple_keywords_together_and_reports_them(
    client,
    monkeypatch,
):
    received: dict[str, object] = {}

    async def fake_search_multiple(queries, max_results, db, *, options):
        received["queries"] = queries
        received["max_results"] = max_results
        received["db"] = db
        received["options"] = options
        return [], [], [], None, 4

    monkeypatch.setattr(
        discovery,
        "search_multiple_with_selected_tools",
        fake_search_multiple,
    )

    response = client.post(
        "/api/v1/discovery/search",
        json={
            "query": "SaaS companies in Austin\nDental clinics in Chicago, SaaS companies in Austin",
            "max_results": 5,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["search_queries"] == [
        "SaaS companies in Austin",
        "Dental clinics in Chicago",
    ]
    assert data["query"] == "SaaS companies in Austin\nDental clinics in Chicago"
    assert data["credits_used"] == 4
    assert received["queries"] == data["search_queries"]
    assert received["max_results"] == 5
    assert received["db"] is not None
    assert received["options"].query == data["query"]


def test_search_multiple_runs_tools_concurrently_and_deduplicates_domains(
    monkeypatch,
):
    active_searches = 0
    maximum_concurrent_searches = 0
    received: list[tuple[str, int]] = []

    class FakeTavilyTool:
        async def search(
            self,
            query,
            max_results,
            _db,
            options=None,
            provider_config=None,
        ):
            nonlocal active_searches, maximum_concurrent_searches
            active_searches += 1
            maximum_concurrent_searches = max(
                maximum_concurrent_searches,
                active_searches,
            )
            await asyncio.sleep(0.01)
            active_searches -= 1
            assert provider_config == "tavily-test-key"
            received.append((query, max_results))
            return discovery_service.ProviderSearchOutcome(
                results=[
                    DiscoveryResult(
                        title=query,
                        url=(
                            "https://same.example.com/"
                            + query.split()[0].lower()
                        ),
                        content="Relevant result",
                        score=0.9,
                    )
                ],
                credits_used=1,
            )

    monkeypatch.setattr(
        discovery_service,
        "get_selected_discovery_tools",
        lambda _db: [DiscoveryProvider.TAVILY],
    )
    monkeypatch.setattr(
        discovery_service,
        "get_tavily_api_key",
        lambda _db: "tavily-test-key",
    )
    monkeypatch.setitem(
        discovery_service.LEAD_DISCOVERY_TOOLS,
        DiscoveryProvider.TAVILY,
        FakeTavilyTool(),
    )

    results, jobs, failures, _answer, credits = asyncio.run(
        discovery_service.search_multiple_with_selected_tools(
            [
                "SaaS companies Austin",
                "Dental clinics Chicago",
            ],
            5,
            None,
            options=TavilyDiscoverySearchRequest(
                query="SaaS companies Austin\nDental clinics Chicago",
                max_results=5,
            ),
        ),
    )

    assert maximum_concurrent_searches == 2
    assert set(received) == {
        ("SaaS companies Austin", 3),
        ("Dental clinics Chicago", 2),
    }
    assert len(results) == 1
    assert jobs == []
    assert failures == []
    assert credits == 2


def test_ai_search_expands_multiple_keywords_within_shared_limit(
    client,
    monkeypatch,
):
    received: list[tuple[str, int]] = []
    active_searches = 0
    maximum_concurrent_searches = 0

    monkeypatch.setattr(
        discovery,
        "get_user_provider_api_key",
        lambda _db, _provider, *, user_id: "ai-test-key",
    )
    monkeypatch.setattr(
        discovery,
        "get_tavily_api_key",
        lambda _db: "tavily-test-key",
    )

    async def fake_expand(keywords, provider, api_key, *, model):
        assert keywords == (
            "IT companies in Denver; SaaS startups in Austin"
        )
        assert provider == "groq"
        assert api_key == "ai-test-key"
        assert model == "openai/gpt-oss-120b"
        return [
            keywords,
            "managed IT providers Denver",
            "Austin SaaS software companies",
        ]

    async def fake_search(
        query,
        _db,
        max_results,
        *,
        options,
        api_key,
        api_key_is_loaded,
    ):
        nonlocal active_searches, maximum_concurrent_searches
        active_searches += 1
        maximum_concurrent_searches = max(
            maximum_concurrent_searches,
            active_searches,
        )
        await asyncio.sleep(0.01)
        active_searches -= 1
        assert api_key == "tavily-test-key"
        assert api_key_is_loaded is True
        received.append((query, max_results))
        index = len(received)
        url = (
            f"https://same.example.com/path-{index}"
            if index == 1
            else f"https://www.same.example.com/path-{index}"
            if index == 2
            else f"https://unique{index}.example.com/"
        )
        return DiscoverySearchResponse(
            query=query,
            results=[
                DiscoveryResult(
                    title=f"{query} company",
                    url=url,
                    content="Relevant result",
                    score=0.9,
                )
            ],
            credits_used=1,
        )

    monkeypatch.setattr(
        discovery,
        "generate_discovery_search_queries",
        fake_expand,
    )
    monkeypatch.setattr(discovery, "search_companies", fake_search)

    response = client.post(
        "/api/v1/discovery/ai-search",
        json={
            "query": "IT companies in Denver\nSaaS startups in Austin",
            "max_results": 5,
            "ai_provider": "groq",
            "model": "openai/gpt-oss-120b",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["search_queries"] == [
        "IT companies in Denver",
        "SaaS startups in Austin",
        "managed IT providers Denver",
        "Austin SaaS software companies",
    ]
    assert len(data["results"]) == 3
    assert data["credits_used"] == 4
    assert maximum_concurrent_searches == 4
    assert received == [
        ("IT companies in Denver", 2),
        ("SaaS startups in Austin", 1),
        ("managed IT providers Denver", 1),
        ("Austin SaaS software companies", 1),
    ]


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
        {
            "query": "first keyword, second keyword, third keyword, fourth keyword, fifth keyword, sixth keyword",
        },
        {
            "query": "valid keyword, no",
        },
        {
            "query": "keyword one, keyword two",
            "max_results": 1,
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
