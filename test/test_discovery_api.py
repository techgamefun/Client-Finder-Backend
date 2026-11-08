from app.schemas.discovery import (
    DiscoveryResult,
    DiscoverySearchResponse,
)
from app.api import discovery


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

    async def fake_search(query: str, max_results: int):
        received["query"] = query
        received["max_results"] = max_results
        return search_response

    monkeypatch.setattr(discovery, "search_companies", fake_search)

    response = client.post(
        "/api/v1/discovery/search",
        json={"query": "manufacturing companies", "max_results": 3},
    )

    assert response.status_code == 200
    assert response.json() == {
        "query": "manufacturing companies",
        "results": [
            {
                "title": "Example Manufacturing",
                "url": "https://example.com/",
                "content": "Manufacturing company",
                "score": 0.9,
            }
        ],
    }
    assert received == {
        "query": "manufacturing companies",
        "max_results": 3,
    }
