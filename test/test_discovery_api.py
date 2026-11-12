from uuid import UUID

from app.schemas.discovery import (
    DiscoveryResult,
    DiscoverySearchResponse,
)
from app.api import discovery
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
    received: dict[str, str | int] = {}

    async def fake_search(query: str, max_results: int, db):
        received["query"] = query
        received["max_results"] = max_results
        return search_response.results, [], []

    monkeypatch.setattr(discovery, "search_with_selected_tools", fake_search)

    response = client.post(
        "/api/v1/discovery/search",
        json={"query": "manufacturing companies", "max_results": 3},
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
            }
        ],
        "search_id": data["search_id"],
        "provider_jobs": [],
        "provider_failures": [],
    }
    UUID(data["search_id"])
    assert received == {
        "query": "manufacturing companies",
        "max_results": 3,
    }

    with TestingSessionLocal() as db:
        search = db.get(SearchKeyword, UUID(data["search_id"]))
        assert search is not None
        assert search.keyword == "manufacturing companies"
        assert search.searched_at is not None

    suggestions = client.get(
        "/api/v1/discovery/suggestions",
        params={"query": "FACTURING"},
    )
    assert suggestions.status_code == 200
    assert suggestions.json()[0]["id"] == data["search_id"]
    assert suggestions.json()[0]["keyword"] == "manufacturing companies"
