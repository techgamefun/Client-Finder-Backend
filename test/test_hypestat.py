import asyncio
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.models import (
    UserProviderCredential,
    Website,
    WebsiteHypeStatFetchJob,
    WebsiteHypeStatProfile,
)
from app.services import hypestat
from app.services import hypestat_usage
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


def test_hypestat_usage_endpoint_returns_official_usage(client, monkeypatch):
    api_key = "test-hypestat-api-key"
    with TestingSessionLocal() as db:
        context = db.info["tenant_context"]
        db.add(
            UserProviderCredential(
                user_id=context.user_id,
                provider="hypestat",
                api_key=api_key,
            ),
        )
        db.commit()

    received_headers = {}

    def handle_request(request):
        received_headers.update(request.headers)
        assert str(request.url) == "https://api.hypestat.com/v1/usage"
        return httpx.Response(
            200,
            json={
                "data": {
                    "key": "hs_live_a1b2...",
                    "limits": {
                        "requests_per_minute": 60,
                        "requests_per_day": 1000,
                        "analyses_per_day": 50,
                    },
                    "used": {
                        "this_minute": 1,
                        "today": 40,
                        "analyses_today": 0,
                    },
                    "resets_at": "2026-10-31T00:00:00Z",
                },
                "meta": {"requests_left_today": 960},
            },
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        hypestat_usage.httpx,
        "AsyncClient",
        lambda timeout: real_async_client(
            transport=httpx.MockTransport(handle_request),
            timeout=timeout,
        ),
    )

    response = client.get("/api/v1/settings/hypestat/usage")

    assert response.status_code == 200
    result = response.json()
    assert result["data"]["used"]["today"] == 40
    assert result["data"]["limits"]["requests_per_day"] == 1000
    assert result["data"]["limits"]["analyses_per_day"] == 50
    assert result["meta"]["requests_left_today"] == 960
    assert "key" not in result["data"]
    assert received_headers["authorization"] == f"Bearer {api_key}"


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


def test_hypestat_profile_jobs_support_search_status_and_pagination(client):
    with TestingSessionLocal() as db:
        jobs = []
        for domain, job_status in (
            ("queue-alpha-one.example", "queued"),
            ("queue-alpha-two.example", "queued"),
            ("queue-alpha-three.example", "failed"),
            ("queue-beta.example", "queued"),
        ):
            website = Website(
                website_url=f"https://{domain}/",
                domain=domain,
            )
            db.add(website)
            db.flush()
            job = WebsiteHypeStatFetchJob(
                website_id=website.id,
                status=job_status,
            )
            db.add(job)
            jobs.append((job, website))
        db.commit()
        expected_job_ids = {str(job.id) for job, _website in jobs[:2]}

    response = client.get(
        "/api/v1/websites/hypestat/jobs",
        params={
            "search": "queue-alpha",
            "status": "queued",
            "page": 2,
            "page_size": 1,
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert len(result["items"]) == 1
    assert result["items"][0]["id"] in expected_job_ids
    assert result["items"][0]["domain"].startswith("queue-alpha")
    assert result["total_count"] == 2
    assert result["page"] == 2
    assert result["page_size"] == 1
    assert result["status_counts"] == {"queued": 2, "failed": 1}


def test_failed_hypestat_profile_jobs_can_be_retried(client, monkeypatch):
    with TestingSessionLocal() as db:
        context = db.info["tenant_context"]
        db.add(
            UserProviderCredential(
                user_id=context.user_id,
                provider="hypestat",
                api_key="test-hypestat-api-key",
            ),
        )
        website = Website(
            website_url="https://retry.example/",
            domain="retry.example",
        )
        db.add(website)
        db.flush()
        profile = WebsiteHypeStatProfile(
            website_id=website.id,
            status="failed",
            error="Temporary upstream failure.",
        )
        job = WebsiteHypeStatFetchJob(
            website_id=website.id,
            status="failed",
            attempts=2,
            error="Temporary upstream failure.",
        )
        db.add_all([profile, job])
        db.commit()
        job_id = job.id
        website_id = website.id

    dispatched_ids = []

    async def dispatch(job_ids):
        dispatched_ids.extend(job_ids)

    monkeypatch.setattr(
        "app.services.website_hypestat_jobs.push_hypestat_fetch_jobs",
        dispatch,
    )

    response = client.post(
        f"/api/v1/websites/hypestat/jobs/{job_id}/retry",
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert dispatched_ids == [str(job_id)]
    with TestingSessionLocal() as db:
        retried_job = db.get(WebsiteHypeStatFetchJob, job_id)
        retried_profile = db.get(WebsiteHypeStatProfile, website_id)
        assert retried_job is not None
        assert retried_job.attempts == 0
        assert retried_job.error is None
        assert retried_profile is not None
        assert retried_profile.status == "pending"
        assert retried_profile.error is None


def test_batch_retry_skips_non_failed_hypestat_jobs(client, monkeypatch):
    with TestingSessionLocal() as db:
        context = db.info["tenant_context"]
        db.add(
            UserProviderCredential(
                user_id=context.user_id,
                provider="hypestat",
                api_key="test-hypestat-api-key",
            ),
        )
        jobs = []
        for domain, job_status in (
            ("retry-one.example", "failed"),
            ("retry-two.example", "queued"),
        ):
            website = Website(
                website_url=f"https://{domain}/",
                domain=domain,
            )
            db.add(website)
            db.flush()
            job = WebsiteHypeStatFetchJob(
                website_id=website.id,
                status=job_status,
            )
            db.add(job)
            jobs.append(job)
        db.commit()
        failed_job_id, queued_job_id = [job.id for job in jobs]

    dispatched_ids = []

    async def dispatch(job_ids):
        dispatched_ids.extend(job_ids)

    monkeypatch.setattr(
        "app.services.website_hypestat_jobs.push_hypestat_fetch_jobs",
        dispatch,
    )

    response = client.post(
        "/api/v1/websites/hypestat/jobs/retry-batch",
        json={"job_ids": [str(failed_job_id), str(queued_job_id)]},
    )

    assert response.status_code == 202
    assert response.json()["retried_job_ids"] == [str(failed_job_id)]
    assert response.json()["failures"] == [
        {
            "job_id": str(queued_job_id),
            "message": "Only failed HypeStat profile jobs can be retried.",
        },
    ]
    assert dispatched_ids == [str(failed_job_id)]


def test_refetch_hypestat_returns_not_found_for_unknown_website(client):
    response = client.post(
        f"/api/v1/websites/{uuid4()}/hypestat/refetch"
    )

    assert response.status_code == 404
