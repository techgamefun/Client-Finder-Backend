import asyncio
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import worker
from app.models import (
    PageSpeedScan,
    PageSpeedScanJob,
    UserProviderCredential,
    Website,
    WebsiteAhrefsDomainRatingJob,
    WebsiteHypeStatFetchJob,
    WebsiteHypeStatProfile,
)
from conftest import TestingSessionLocal


def _save_provider_key(provider: str) -> None:
    with TestingSessionLocal() as db:
        context = db.info["tenant_context"]
        db.add(
            UserProviderCredential(
                user_id=context.user_id,
                provider=provider,
                api_key=f"test-{provider}-key",
            )
        )
        db.commit()


def _create_website(db, domain: str) -> Website:
    website = Website(
        website_url=f"https://{domain}/",
        domain=domain,
    )
    db.add(website)
    db.flush()
    return website


async def _no_enqueue(_job_ids) -> None:
    return None


def test_bulk_ahrefs_queues_only_missing_ratings_without_active_jobs(
    client,
    monkeypatch,
):
    _save_provider_key("ahrefs")
    monkeypatch.setattr(
        "app.services.website_ahrefs_jobs.push_ahrefs_domain_rating_jobs",
        _no_enqueue,
    )
    with TestingSessionLocal() as db:
        missing = _create_website(db, "missing-rating.example")
        active = _create_website(db, "active-rating.example")
        active_job = WebsiteAhrefsDomainRatingJob(
            website_id=active.id,
            source_crawled_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
        )
        db.add(active_job)
        db.commit()
        missing_id = missing.id
        active_id = active.id

    response = client.post("/api/v1/websites/ahrefs-domain-rating/fetch-missing")

    assert response.status_code == 202
    assert response.json() == {
        "website_count": 2,
        "queued_website_count": 1,
        "queued_job_count": 1,
        "missing_website_count": 2,
        "in_progress_job_count": 1,
        "skipped_website_count": 1,
    }
    with TestingSessionLocal() as db:
        jobs = db.scalars(select(WebsiteAhrefsDomainRatingJob)).all()
        assert len(jobs) == 2
        assert sum(job.website_id == missing_id for job in jobs) == 1
        assert sum(job.website_id == active_id for job in jobs) == 1


def test_bulk_hypestat_queues_missing_profiles_and_skips_active_or_complete(
    client,
    monkeypatch,
):
    _save_provider_key("hypestat")
    monkeypatch.setattr(
        "app.config.settings.crawler_service_shared_token",
        "test-crawler-token",
    )
    monkeypatch.setattr(
        "app.services.website_hypestat_jobs.push_hypestat_fetch_jobs",
        _no_enqueue,
    )
    with TestingSessionLocal() as db:
        missing = _create_website(db, "missing-hypestat.example")
        complete = _create_website(db, "complete-hypestat.example")
        active = _create_website(db, "active-hypestat.example")
        db.add(
            WebsiteHypeStatProfile(
                website_id=complete.id,
                status="succeeded",
                payload={"entry": {"status": "ok"}},
            )
        )
        db.add(
            WebsiteHypeStatFetchJob(
                website_id=active.id,
                status="queued",
            )
        )
        db.commit()
        missing_id = missing.id
        complete_id = complete.id
        active_id = active.id

    response = client.post("/api/v1/websites/hypestat/fetch-missing")

    assert response.status_code == 202
    assert response.json() == {
        "website_count": 3,
        "queued_website_count": 1,
        "queued_job_count": 1,
        "missing_website_count": 2,
        "in_progress_job_count": 1,
        "skipped_website_count": 2,
    }
    with TestingSessionLocal() as db:
        jobs = db.scalars(select(WebsiteHypeStatFetchJob)).all()
        assert len(jobs) == 2
        assert sum(job.website_id == missing_id for job in jobs) == 1
        assert sum(job.website_id == complete_id for job in jobs) == 0
        assert sum(job.website_id == active_id for job in jobs) == 1
        profile = db.get(WebsiteHypeStatProfile, missing_id)
        assert profile is not None
        assert profile.status == "pending"


def test_bulk_pagespeed_queues_only_missing_strategies(
    client,
    monkeypatch,
):
    _save_provider_key("pagespeed")
    monkeypatch.setattr(
        "app.services.pagespeed_jobs.push_pagespeed_jobs",
        _no_enqueue,
    )
    with TestingSessionLocal() as db:
        partial = _create_website(db, "partial-pagespeed.example")
        active = _create_website(db, "active-pagespeed.example")
        complete = _create_website(db, "complete-pagespeed.example")
        db.add(
            PageSpeedScan(
                website_id=partial.id,
                website_url=partial.website_url,
                strategy="mobile",
                category_scores={},
            )
        )
        db.add(
            PageSpeedScanJob(
                website_id=active.id,
                website_url=active.website_url,
                strategy="desktop",
            )
        )
        for strategy in ("mobile", "desktop"):
            db.add(
                PageSpeedScan(
                    website_id=complete.id,
                    website_url=complete.website_url,
                    strategy=strategy,
                    category_scores={},
                )
            )
        db.commit()
        partial_id = partial.id
        active_id = active.id
        complete_id = complete.id

    response = client.post("/api/v1/websites/pagespeed/fetch-missing")

    assert response.status_code == 202
    assert response.json() == {
        "website_count": 3,
        "queued_website_count": 2,
        "queued_job_count": 2,
        "missing_website_count": 2,
        "in_progress_job_count": 1,
        "skipped_website_count": 1,
    }
    with TestingSessionLocal() as db:
        jobs = db.scalars(
            select(PageSpeedScanJob).where(
                PageSpeedScanJob.status == "queued"
            )
        ).all()
        assert {(job.website_id, job.strategy) for job in jobs} == {
            (partial_id, "desktop"),
            (active_id, "mobile"),
            (active_id, "desktop"),
        }
        assert all(job.website_id != complete_id for job in jobs)


def test_bulk_ahrefs_reports_missing_ratings_already_in_progress(
    client,
):
    _save_provider_key("ahrefs")
    with TestingSessionLocal() as db:
        website = _create_website(db, "active-only-rating.example")
        db.add(
            WebsiteAhrefsDomainRatingJob(
                website_id=website.id,
                source_crawled_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
            )
        )
        db.commit()

    response = client.post("/api/v1/websites/ahrefs-domain-rating/fetch-missing")

    assert response.status_code == 202
    assert response.json() == {
        "website_count": 1,
        "queued_website_count": 0,
        "queued_job_count": 0,
        "missing_website_count": 1,
        "in_progress_job_count": 1,
        "skipped_website_count": 1,
    }


def test_website_enrichment_counts_cover_missing_data_across_all_websites(
    client,
):
    with TestingSessionLocal() as db:
        missing = _create_website(db, "missing-counts.example")
        complete = _create_website(db, "complete-counts.example")
        partial = _create_website(db, "partial-counts.example")
        complete.ahrefs_domain_rating = 42
        db.add(
            WebsiteHypeStatProfile(
                website_id=complete.id,
                status="succeeded",
                payload={"entry": {"status": "ok"}},
            )
        )
        db.add(
            WebsiteAhrefsDomainRatingJob(
                website_id=missing.id,
                source_crawled_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
            )
        )
        db.add(
            WebsiteHypeStatFetchJob(
                website_id=missing.id,
                status="running",
            )
        )
        db.add(
            PageSpeedScanJob(
                website_id=missing.id,
                website_url=missing.website_url,
                strategy="desktop",
                status="running",
            )
        )
        for strategy in ("desktop", "mobile"):
            db.add(
                PageSpeedScan(
                    website_id=complete.id,
                    website_url=complete.website_url,
                    strategy=strategy,
                    category_scores={},
                )
            )
        db.add(
            PageSpeedScan(
                website_id=partial.id,
                website_url=partial.website_url,
                strategy="mobile",
                category_scores={},
            )
        )
        db.commit()

    response = client.get("/api/v1/websites/enrichment-counts")

    assert response.status_code == 200
    assert response.json() == {
        "classification_missing_count": 0,
        "ahrefs_missing_count": 2,
        "hypestat_missing_count": 2,
        "pagespeed_missing_scan_count": 3,
    }


def test_hypestat_worker_fetches_and_persists_queued_profile(
    client,
    monkeypatch,
):
    _save_provider_key("hypestat")
    monkeypatch.setattr(
        "app.config.settings.crawler_service_shared_token",
        "test-crawler-token",
    )
    with TestingSessionLocal() as db:
        website = _create_website(db, "worker-hypestat.example")
        job = WebsiteHypeStatFetchJob(website_id=website.id)
        db.add(job)
        db.commit()
        website_id = website.id
        job_id = job.id

    worker_session_factory = sessionmaker(
        bind=TestingSessionLocal.kw["bind"],
        autoflush=False,
        expire_on_commit=False,
    )
    monkeypatch.setattr(worker, "SessionLocal", worker_session_factory)

    def handle_request(request):
        assert request.url.path == "/hypestat/domains"
        assert request.headers["x-hypestat-api-key"] == "test-hypestat-key"
        assert request.headers["x-crawler-service-token"] == (
            "test-crawler-token"
        )
        assert request.read().decode() == (
            '{"domains":["worker-hypestat.example"]}'
        )
        return httpx.Response(
            200,
            json={
                "data": {
                    "domains": [
                        {
                            "domain": "worker-hypestat.example",
                            "status": "ok",
                            "data": {"monthly_visits": 1200},
                        }
                    ]
                },
                "meta": {},
            },
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.hypestat.httpx.AsyncClient",
        lambda timeout: real_async_client(
            transport=httpx.MockTransport(handle_request),
            timeout=timeout,
        ),
    )

    asyncio.run(worker.process_hypestat_fetch_job(job_id))

    with TestingSessionLocal() as db:
        saved_job = db.get(WebsiteHypeStatFetchJob, job_id)
        profile = db.get(WebsiteHypeStatProfile, website_id)
        assert saved_job is not None
        assert saved_job.status == "succeeded"
        assert saved_job.attempts == 1
        assert profile is not None
        assert profile.status == "succeeded"
        assert profile.payload is not None
        assert profile.payload["entry"]["data"]["monthly_visits"] == 1200
