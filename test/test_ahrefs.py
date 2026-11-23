import httpx
from sqlalchemy import select

from app.models import UserProviderCredential, Website, WebsiteAhrefsMetrics
from conftest import TestingSessionLocal


def _save_ahrefs_key() -> str:
    api_key = "test-ahrefs-api-key"
    with TestingSessionLocal() as db:
        context = db.info["tenant_context"]
        db.add(
            UserProviderCredential(
                user_id=context.user_id,
                provider="ahrefs",
                api_key=api_key,
            )
        )
        db.commit()
    return api_key


def test_ahrefs_api_key_is_saved_without_being_returned(client):
    api_key = "test-ahrefs-settings-key"

    response = client.put(
        "/api/v1/settings",
        json={"ahrefs_api_key": api_key},
    )

    assert response.status_code == 200
    assert response.json()["ahrefs_configured"] is True
    assert api_key not in response.text
    with TestingSessionLocal() as db:
        context = db.info["tenant_context"]
        credential = db.scalar(
            select(UserProviderCredential).where(
                UserProviderCredential.user_id == context.user_id,
                UserProviderCredential.provider == "ahrefs",
            )
        )
        assert credential is not None
        assert credential.api_key == api_key


def test_ahrefs_usage_returns_subscription_limits(client, monkeypatch):
    api_key = _save_ahrefs_key()
    received_headers = {}

    def handle_request(request):
        received_headers.update(request.headers)
        assert request.url.path == "/v3/subscription-info/limits-and-usage"
        assert request.url.params["output"] == "json"
        return httpx.Response(
            200,
            json={
                "limits_and_usage": {
                    "api_key_expiration_date": "2027-01-01T00:00:00Z",
                    "subscription": "Standard",
                    "units_limit_api_key": 100000,
                    "units_limit_workspace": 500000,
                    "units_usage_api_key": 4200,
                    "units_usage_workspace": 12500,
                    "usage_reset_date": "2026-12-01",
                }
            },
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.ahrefs.httpx.AsyncClient",
        lambda timeout: real_async_client(
            transport=httpx.MockTransport(handle_request),
            timeout=timeout,
        ),
    )

    response = client.get("/api/v1/settings/ahrefs/usage")

    assert response.status_code == 200
    result = response.json()["limits_and_usage"]
    assert result["units_usage_api_key"] == 4200
    assert result["units_limit_api_key"] == 100000
    assert result["units_usage_workspace"] == 12500
    assert result["usage_reset_date"] == "2026-12-01"
    assert api_key not in response.text
    assert received_headers["authorization"] == f"Bearer {api_key}"


def test_ahrefs_usage_is_unavailable_for_public_only_key(client, monkeypatch):
    _save_ahrefs_key()
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.ahrefs.httpx.AsyncClient",
        lambda timeout: real_async_client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(403)
            ),
            timeout=timeout,
        ),
    )

    response = client.get("/api/v1/settings/ahrefs/usage")

    assert response.status_code == 200
    assert response.json() is None


def test_ahrefs_site_metrics_are_cached_and_returned_in_website_detail(
    client,
    monkeypatch,
):
    api_key = _save_ahrefs_key()
    with TestingSessionLocal() as db:
        website = Website(
            website_url="https://ahrefs-test.example/",
            domain="ahrefs-test.example",
        )
        db.add(website)
        db.commit()
        website_id = website.id

    requests = []

    def handle_request(request):
        requests.append(request)
        assert request.headers["authorization"] == f"Bearer {api_key}"
        if request.url.path.endswith("/public/domain-rating-free"):
            assert request.url.params["target"] == "https://ahrefs-test.example"
            return httpx.Response(
                200,
                json={
                    "domain_rating": {
                        "domain_rating": 42.5,
                        "license": "https://ahrefs.com/license",
                    }
                },
            )
        if request.url.path.endswith("/site-explorer/metrics"):
            assert request.url.params["date"]
            assert request.url.params["mode"] == "subdomains"
            return httpx.Response(
                200,
                headers={"x-api-units-cost-total-actual": "53"},
                json={
                    "metrics": {
                        "org_traffic": 1200,
                        "org_cost": 6400,
                        "org_keywords": 85,
                    }
                },
            )
        if request.url.path.endswith("/site-explorer/backlinks-stats"):
            return httpx.Response(
                200,
                headers={"x-api-units-cost-total-actual": "50"},
                json={
                    "metrics": {
                        "all_time": 250,
                        "all_time_refdomains": 22,
                        "live": 180,
                        "live_refdomains": 17,
                    }
                },
            )
        raise AssertionError(f"Unexpected Ahrefs request: {request.url}")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.ahrefs.httpx.AsyncClient",
        lambda timeout: real_async_client(
            transport=httpx.MockTransport(handle_request),
            timeout=timeout,
        ),
    )

    first_response = client.post(
        f"/api/v1/websites/{website_id}/ahrefs-metrics"
    )

    assert first_response.status_code == 200
    result = first_response.json()
    assert result["domain_rating"] == 42.5
    assert result["domain_rating_license_url"] == "https://ahrefs.com/license"
    assert result["organic_traffic"] == 1200
    assert result["organic_traffic_cost_cents"] == 6400
    assert result["organic_keywords"] == 85
    assert result["backlinks_live"] == 180
    assert result["refdomains_all_time"] == 22
    assert result["units_used"] == 103
    assert len(requests) == 3

    cached_response = client.post(
        f"/api/v1/websites/{website_id}/ahrefs-metrics"
    )
    detail_response = client.get(f"/api/v1/websites/{website_id}")

    assert cached_response.status_code == 200
    assert detail_response.status_code == 200
    assert detail_response.json()["ahrefs_metrics"]["domain_rating"] == 42.5
    assert detail_response.json()["website"]["ahrefs_domain_rating"] == 42.5
    assert len(requests) == 3
    with TestingSessionLocal() as db:
        metrics = db.scalar(
            select(WebsiteAhrefsMetrics).where(
                WebsiteAhrefsMetrics.website_id == website_id
            )
        )
        assert metrics is not None
        assert metrics.units_used == 103


def test_ahrefs_free_key_returns_domain_rating_without_paid_metrics(
    client,
    monkeypatch,
):
    api_key = _save_ahrefs_key()
    with TestingSessionLocal() as db:
        website = Website(
            website_url="https://ahrefs-free.example/",
            domain="ahrefs-free.example",
        )
        db.add(website)
        db.commit()
        website_id = website.id

    requested_paths = []

    def handle_request(request):
        requested_paths.append(request.url.path)
        assert request.headers["authorization"] == f"Bearer {api_key}"
        if request.url.path.endswith("/public/domain-rating-free"):
            return httpx.Response(
                200,
                json={
                    "domain_rating": {
                        "domain_rating": 35.0,
                        "license": "https://ahrefs.com/license",
                    }
                },
            )
        if request.url.path.endswith("/site-explorer/metrics"):
            return httpx.Response(403)
        raise AssertionError(f"Unexpected Ahrefs request: {request.url}")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.ahrefs.httpx.AsyncClient",
        lambda timeout: real_async_client(
            transport=httpx.MockTransport(handle_request),
            timeout=timeout,
        ),
    )

    response = client.post(
        f"/api/v1/websites/{website_id}/ahrefs-metrics"
    )

    assert response.status_code == 200
    result = response.json()
    assert result["domain_rating"] == 35.0
    assert "organic_traffic" not in result
    assert "backlinks_live" not in result
    assert "units_used" not in result
    assert requested_paths == [
        "/v3/public/domain-rating-free",
        "/v3/site-explorer/metrics",
    ]
    detail_response = client.get(f"/api/v1/websites/{website_id}")
    assert detail_response.status_code == 200
    assert detail_response.json()["ahrefs_metrics"] is None
    assert detail_response.json()["website"]["ahrefs_domain_rating"] == 35.0
    with TestingSessionLocal() as db:
        assert db.scalar(
            select(WebsiteAhrefsMetrics).where(
                WebsiteAhrefsMetrics.website_id == website_id
            )
        ) is None
        website = db.get(Website, website_id)
        assert website is not None
        assert website.ahrefs_domain_rating_license_url == (
            "https://ahrefs.com/license"
        )
