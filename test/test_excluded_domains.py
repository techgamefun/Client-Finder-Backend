from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select

from conftest import TestingSessionLocal
from app.models import CrawlerDomainFailure, ExcludedDomain, WebsiteCrawlJob
from app.services import crawl_jobs
from app.services.excluded_domains import (
    SYSTEM_PRESET_DOMAINS,
    find_excluded_domain,
    record_unreachable_domain_failure,
)
from app.tenant_context import TenantContext


def test_seeded_domains_are_listed_and_can_be_filtered(client):
    response = client.get("/api/v1/excluded-domains")
    filtered = client.get(
        "/api/v1/excluded-domains",
        params={"reason": "Big Tech / Social", "search": "youtube"},
    )
    count = client.get(
        "/api/v1/excluded-domains/count",
        params={"reason": "Big Tech / Social", "search": "youtube"},
    )

    assert response.status_code == 200
    assert {item["domain"] for item in response.json()} == set(
        SYSTEM_PRESET_DOMAINS
    )
    assert all(
        item["source"] == "system_preset"
        and item["reason"] == "Big Tech / Social"
        for item in response.json()
    )
    assert filtered.status_code == 200
    assert [item["domain"] for item in filtered.json()] == ["youtube.com"]
    assert count.status_code == 200
    assert count.json() == {"total_count": 1}
    first_page = client.get(
        "/api/v1/excluded-domains",
        params={"limit": 1, "offset": 0},
    )
    second_page = client.get(
        "/api/v1/excluded-domains",
        params={"limit": 1, "offset": 1},
    )
    assert first_page.status_code == second_page.status_code == 200
    assert len(first_page.json()) == len(second_page.json()) == 1
    assert first_page.json()[0]["domain"] != second_page.json()[0]["domain"]


def test_excluded_domain_batch_accepts_1000_and_deduplicates(client):
    with TestingSessionLocal() as db:
        queued_job = WebsiteCrawlJob(
            website_url="https://shop.pending.example/",
            status="queued",
        )
        db.add(queued_job)
        db.commit()
        queued_job_id = queued_job.id

    domains = [
        {
            "domain": "https://www.pending.example/listing",
            "reason": "Manual Exclusion",
        },
        {
            "domain": "pending.example",
            "reason": "Unreachable",
        },
        {
            "domain": "google.com",
            "reason": "Manual Exclusion",
            "note": "User reviewed this preset.",
        },
    ]
    domains.extend(
        {
            "domain": f"bulk-{index}.example",
            "reason": "Manual Exclusion",
        }
        for index in range(997)
    )
    response = client.post(
        "/api/v1/excluded-domains/batch",
        json={"domains": domains},
    )

    assert response.status_code == 200
    assert response.json() == {
        "requested_count": 1000,
        "created_count": 998,
        "updated_count": 1,
        "duplicate_count": 1,
    }
    with TestingSessionLocal() as db:
        saved_job = db.get(WebsiteCrawlJob, queued_job_id)
        assert saved_job is None
        google = db.scalar(
            select(ExcludedDomain).where(ExcludedDomain.domain == "google.com")
        )
        assert google.reason == "Manual Exclusion"
        assert google.note == "User reviewed this preset."


def test_excluded_domain_batch_rejects_more_than_1000(client):
    response = client.post(
        "/api/v1/excluded-domains/batch",
        json={
            "domains": [
                {
                    "domain": f"bulk-{index}.example",
                    "reason": "Manual Exclusion",
                }
                for index in range(1001)
            ]
        },
    )

    assert response.status_code == 422


def test_exclusion_normalizes_urls_matches_subdomains_and_can_be_removed(
    client,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.crawl_jobs.enqueue_job",
        _noop_enqueue_job,
    )
    created = client.post(
        "/api/v1/excluded-domains",
        json={
            "domain": "https://www.Facebook.com/some/page?id=1",
            "reason": "Manual Exclusion",
            "note": "User requested this.",
        },
    )

    assert created.status_code == 201
    domain = created.json()
    assert domain["domain"] == "facebook.com"
    assert domain["source"] == "user"

    excluded_job = client.post(
        "/api/v1/crawler/crawl",
        json={"website_url": "https://m.facebook.com/about"},
    )
    assert excluded_job.status_code == 409
    assert excluded_job.json()["detail"] == (
        "This domain is excluded: Manual Exclusion."
    )

    unrelated_job = client.post(
        "/api/v1/crawler/crawl",
        json={"website_url": "https://notfacebook.com/"},
    )
    assert unrelated_job.status_code == 202
    assert unrelated_job.json()["status"] == "queued"

    deleted = client.delete(
        f"/api/v1/excluded-domains/{domain['id']}",
    )
    assert deleted.status_code == 204

    crawlable_job = client.post(
        "/api/v1/crawler/crawl",
        json={"website_url": "https://m.facebook.com/about"},
    )
    assert crawlable_job.status_code == 202
    assert crawlable_job.json()["status"] == "queued"
    assert "skip_reason" not in crawlable_job.json()

    with TestingSessionLocal() as db:
        assert db.scalar(
            select(CrawlerDomainFailure).where(
                CrawlerDomainFailure.domain == "facebook.com"
            )
        ) is None


def test_adding_exclusion_deletes_existing_queued_jobs(client, monkeypatch):
    monkeypatch.setattr(
        "app.services.crawl_jobs.enqueue_job",
        _noop_enqueue_job,
    )
    job = client.post(
        "/api/v1/crawler/crawl",
        json={"website_url": "https://pending.example/"},
    ).json()

    response = client.post(
        "/api/v1/excluded-domains",
        json={
            "domain": "example",
            "reason": "Manual Exclusion",
        },
    )

    assert response.status_code == 422

    response = client.post(
        "/api/v1/excluded-domains",
        json={
            "domain": "pending.example",
            "reason": "Manual Exclusion",
        },
    )
    assert response.status_code == 201
    with TestingSessionLocal() as db:
        saved_job = db.get(WebsiteCrawlJob, UUID(job["id"]))
        assert saved_job is None


def test_adding_exclusion_deletes_queued_and_failed_jobs_only(client):
    with TestingSessionLocal() as db:
        jobs = {
            job.status: job
            for job in (
                WebsiteCrawlJob(
                    website_url=f"https://{status}.example.test/",
                    status=status,
                )
                for status in ("queued", "failed", "running", "succeeded")
            )
        }
        db.add_all(jobs.values())
        db.commit()
        job_ids = {status: job.id for status, job in jobs.items()}

    response = client.post(
        "/api/v1/excluded-domains",
        json={
            "domain": "example.test",
            "reason": "Manual Exclusion",
        },
    )

    assert response.status_code == 201
    with TestingSessionLocal() as db:
        assert db.get(WebsiteCrawlJob, job_ids["queued"]) is None
        assert db.get(WebsiteCrawlJob, job_ids["failed"]) is None
        assert db.get(WebsiteCrawlJob, job_ids["running"]) is not None
        assert db.get(WebsiteCrawlJob, job_ids["succeeded"]) is not None


def test_batch_crawl_reports_excluded_targets_without_creating_jobs(
    client,
    monkeypatch,
):
    dispatch_calls: list[list[str]] = []

    async def capture_enqueue(job_ids: list[str]) -> None:
        dispatch_calls.append(job_ids)

    monkeypatch.setattr(crawl_jobs, "enqueue_crawl_jobs", capture_enqueue)
    exclusion = client.post(
        "/api/v1/excluded-domains",
        json={
            "domain": "blocked.example",
            "reason": "Manual Exclusion",
        },
    )
    assert exclusion.status_code == 201

    response = client.post(
        "/api/v1/crawler/crawl/batch",
        json={
            "website_urls": [
                "https://shop.blocked.example/",
                "https://allowed.example/",
            ]
        },
    )

    assert response.status_code == 202
    assert [job["website_url"] for job in response.json()["jobs"]] == [
        "https://allowed.example/"
    ]
    assert response.json()["excluded"] == [
        {
            "website_url": "https://shop.blocked.example/",
            "reason": "Manual Exclusion",
        }
    ]
    assert len(dispatch_calls) == 1
    assert len(dispatch_calls[0]) == 1
    with TestingSessionLocal() as db:
        jobs = db.scalars(select(WebsiteCrawlJob)).all()
        assert len(jobs) == 1
        assert jobs[0].website_url == "https://allowed.example/"
        assert jobs[0].status == "queued"


def test_auto_excludes_domain_after_three_terminal_failures(client):
    with TestingSessionLocal() as db:
        job = WebsiteCrawlJob(
            website_url="https://shop.unreachable.example/",
            status="queued",
        )
        db.add(job)
        db.commit()
        assert find_excluded_domain(
            db,
            "https://unreachable.example/",
        ) is None

        assert (
            record_unreachable_domain_failure(
                db,
                "https://www.unreachable.example/",
                "DNS lookup failed.",
            )
            == 1
        )
        assert find_excluded_domain(
            db,
            "https://unreachable.example/",
        ) is None
        assert (
            record_unreachable_domain_failure(
                db,
                "https://unreachable.example/",
                "DNS lookup failed again.",
            )
            == 2
        )
        assert (
            record_unreachable_domain_failure(
                db,
                "https://unreachable.example/",
                "DNS lookup failed for the third time.",
            )
            == 3
        )

        exclusion = find_excluded_domain(
            db,
            "https://shop.unreachable.example/",
        )
        assert exclusion is not None
        assert exclusion.reason == "Unreachable"
        assert exclusion.source == "auto_crawler"
        assert exclusion.failure_count == 3
        assert db.get(WebsiteCrawlJob, job.id) is None


async def _noop_enqueue_job(_job_id: str) -> None:
    return None
