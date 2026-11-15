import asyncio
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.models import Website, WebsiteHypeStatProfile
from app.services import hypestat
from app.services.hypestat import (
    HypeStatFetchResult,
    _parse_domain_results,
)
from conftest import TestingSessionLocal


def test_parse_bulk_response_preserves_full_profiles_and_queued_status():
    meta = {"requests_left_today": 998}
    profile = {
        "domain": "known.example",
        "status": "ok",
        "data": {
            "traffic": {"visits_daily": 1200},
            "whois": {"registrar": "Example Registrar"},
        },
    }
    queued = {
        "domain": "new.example",
        "status": "not_analyzed_yet",
        "message": "new.example is queued for analysis.",
    }

    results = _parse_domain_results(
        ["known.example", "new.example"],
        [profile, queued],
        meta,
    )

    assert results["known.example"].status == "succeeded"
    assert results["known.example"].payload == {
        "entry": profile,
        "meta": meta,
    }
    assert results["new.example"].status == "queued"
    assert results["new.example"].payload == {
        "entry": queued,
        "meta": meta,
    }


def test_parse_bulk_response_reports_missing_domains():
    results = _parse_domain_results(
        ["missing.example"],
        [],
        {"requests_left_today": 999},
    )

    assert results["missing.example"].status == "failed"
    assert results["missing.example"].error == (
        "HypeStat response omitted this domain."
    )


def test_fetch_profiles_forwards_settings_key_only_to_crawler_service(
    monkeypatch,
):
    received_headers = {}
    response_body = {
        "data": {
            "domains": [
                {
                    "domain": "known.example",
                    "status": "ok",
                    "data": {"domain": {"name": "known.example"}},
                },
            ],
        },
        "meta": {"requests_left_today": 999},
    }

    def handle_request(request):
        received_headers.update(request.headers)
        return httpx.Response(200, json=response_body)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        hypestat.httpx,
        "AsyncClient",
        lambda timeout: real_async_client(
            transport=httpx.MockTransport(handle_request),
            timeout=timeout,
        ),
    )
    monkeypatch.setattr(
        hypestat.settings,
        "crawler_service_shared_token",
        "test-shared-token",
    )

    results = asyncio.run(
        hypestat.fetch_hypestat_profiles(
            ["known.example"],
            "test-hypestat-api-key",
        )
    )

    assert results["known.example"].status == "succeeded"
    assert received_headers["x-hypestat-api-key"] == "test-hypestat-api-key"
    assert received_headers["x-crawler-service-token"] == "test-shared-token"


def test_refetch_hypestat_endpoint_updates_saved_profile(client, monkeypatch):
    website_id = uuid4()
    with TestingSessionLocal() as db:
        db.add(
            Website(
                id=website_id,
                website_url="https://refetch.example/",
                domain="refetch.example",
            )
        )
        db.commit()

    async def fake_fetch_profiles(domains, _api_key):
        assert domains == ["refetch.example"]
        return {
            "refetch.example": HypeStatFetchResult(
                status="succeeded",
                payload={"entry": {"domain": "refetch.example", "status": "ok"}},
            ),
        }

    monkeypatch.setattr(
        "app.services.website_hypestat.fetch_hypestat_profiles",
        fake_fetch_profiles,
    )
    response = client.post(
        f"/api/v1/websites/{website_id}/hypestat/refetch"
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "succeeded"
    assert result["payload"]["entry"]["domain"] == "refetch.example"
    assert result["checked_at"] is not None
    assert result["fetched_at"] is not None
    with TestingSessionLocal() as db:
        profile = db.scalar(
            select(WebsiteHypeStatProfile).where(
                WebsiteHypeStatProfile.website_id == website_id
            )
        )
        assert profile is not None
        assert profile.status == "succeeded"


def test_refetch_hypestat_returns_not_found_for_unknown_website(client):
    response = client.post(
        f"/api/v1/websites/{uuid4()}/hypestat/refetch"
    )

    assert response.status_code == 404
