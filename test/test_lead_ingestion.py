import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from conftest import TestingSessionLocal
from app.models import Company, LeadSource
from app.schemas.discovery import DiscoveryResult, DiscoverySearchResponse
from app.services import lead_ingestion


def _search_response(*results: tuple[str, str, str]) -> DiscoverySearchResponse:
    return DiscoverySearchResponse(
        query="manufacturing companies",
        results=[
            DiscoveryResult(
                title=title,
                url=url,
                content=evidence,
                score=0.9,
            )
            for title, url, evidence in results
        ],
    )


def _mock_search(monkeypatch, response: DiscoverySearchResponse) -> None:
    async def fake_search(query: str, max_results: int):
        return response

    monkeypatch.setattr(lead_ingestion, "search_companies", fake_search)


def test_ingestion_deduplicates_company_name_and_source_url(client, monkeypatch):
    response = _search_response(
        ("Acme, Inc.", "https://example.com/acme", "Acme evidence"),
        (" ACME Inc ", "https://example.com/acme/about", "More evidence"),
        ("Another Company", "https://example.com/acme", "Repeated URL"),
    )
    _mock_search(monkeypatch, response)

    result = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert result.status_code == 200
    assert result.json() == {
        "query": "manufacturing companies",
        "results_found": 3,
        "candidates_created": 1,
        "sources_added": 2,
        "results_skipped": 1,
    }

    with TestingSessionLocal() as db:
        companies = db.scalars(select(Company)).all()
        sources = db.scalars(select(LeadSource)).all()

    assert len(companies) == 1
    assert companies[0].normalized_name == "acme inc"
    assert companies[0].lead_status == "needs_review"
    assert len(sources) == 2
    assert {source.company_id for source in sources} == {companies[0].id}


def test_reingesting_existing_source_skips_it(client, monkeypatch):
    response = _search_response(
        ("Example Company", "https://example.com/company", "Evidence"),
    )
    _mock_search(monkeypatch, response)

    first = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )
    second = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert first.status_code == 200
    assert first.json()["sources_added"] == 1
    assert second.status_code == 200
    assert second.json()["candidates_created"] == 0
    assert second.json()["sources_added"] == 0
    assert second.json()["results_skipped"] == 1

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 1
        assert db.scalar(select(func.count()).select_from(LeadSource)) == 1


def test_ingestion_attaches_new_source_to_existing_company(client, monkeypatch):
    created = client.post(
        "/api/v1/companies",
        json={"name": "Example Manufacturing, Inc."},
    )
    assert created.status_code == 201

    _mock_search(
        monkeypatch,
        _search_response(
            (
                " example manufacturing inc ",
                "https://example.com/manufacturing",
                "Search evidence",
            ),
        ),
    )
    response = client.post(
        "/api/v1/discovery/ingest",
        json={"query": "manufacturing companies"},
    )

    assert response.status_code == 200
    assert response.json()["candidates_created"] == 0
    assert response.json()["sources_added"] == 1

    with TestingSessionLocal() as db:
        companies = db.scalars(select(Company)).all()
        sources = db.scalars(select(LeadSource)).all()

    assert len(companies) == 1
    assert companies[0].lead_status == "new"
    assert len(sources) == 1
    assert sources[0].company_id == companies[0].id


def test_ingestion_rolls_back_whole_batch_on_commit_failure(
    client,
    monkeypatch,
):
    response = _search_response(
        ("First Company", "https://example.com/first", "First"),
        ("Second Company", "https://example.com/second", "Second"),
    )
    _mock_search(monkeypatch, response)

    def fail_commit(session: Session) -> None:
        raise RuntimeError("database commit failed")

    event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="database commit failed"):
            client.post(
                "/api/v1/discovery/ingest",
                json={"query": "manufacturing companies"},
            )
    finally:
        event.remove(Session, "before_commit", fail_commit)

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 0
        assert db.scalar(select(func.count()).select_from(LeadSource)) == 0
