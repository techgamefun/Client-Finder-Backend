import asyncio
from uuid import uuid4

import pytest

from app import main, worker
from app.http_clients import HTTPClientPool


def test_http_client_pool_reuses_clients_and_closes_them():
    async def exercise_pool():
        pool = HTTPClientPool()

        async_client = pool.async_client(timeout=10.0)
        sync_client = pool.sync_client(timeout=10.0)

        assert pool.async_client(timeout=10.0) is async_client
        assert pool.sync_client(timeout=10.0) is sync_client
        assert pool.async_client(timeout=20.0) is not async_client

        other_async_client = pool.async_client(timeout=20.0)
        await pool.aclose()

        assert async_client.is_closed
        assert other_async_client.is_closed
        assert sync_client.is_closed

    asyncio.run(exercise_pool())


def test_lifespan_starts_and_stops_in_process_worker(monkeypatch):
    monkeypatch.setattr(main.settings, "run_worker_in_api", True)
    monkeypatch.setattr(main, "should_auto_migrate", lambda: False)

    async def exercise_lifespan():
        worker_started = asyncio.Event()
        worker_stopped = asyncio.Event()

        async def fake_worker():
            worker_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                worker_stopped.set()

        monkeypatch.setattr(worker, "run_worker", fake_worker)

        async with main.lifespan(main.app):
            await asyncio.wait_for(worker_started.wait(), timeout=1)
            assert not worker_stopped.is_set()

        assert worker_stopped.is_set()

    asyncio.run(exercise_lifespan())


def test_worker_runs_configured_number_of_crawls_concurrently(monkeypatch):
    concurrency = 5
    job_ids = [str(uuid4()) for _ in range(concurrency)]
    started = asyncio.Event()
    release = asyncio.Event()
    current_jobs = 0
    maximum_jobs = 0

    async def dequeue_crawl_job():
        return job_ids.pop(0) if job_ids else None

    async def no_job():
        return None

    async def no_work():
        return False

    async def process_crawl_job(_job_id):
        nonlocal current_jobs, maximum_jobs
        current_jobs += 1
        maximum_jobs = max(maximum_jobs, current_jobs)
        if current_jobs == concurrency:
            started.set()
        try:
            await release.wait()
        finally:
            current_jobs -= 1

    monkeypatch.setattr(worker, "validate_redis_configuration", lambda: None)
    monkeypatch.setattr(worker, "dequeue_job", dequeue_crawl_job)
    monkeypatch.setattr(worker, "dequeue_pagespeed_job", no_job)
    monkeypatch.setattr(worker, "dequeue_corpus_analysis_job", no_job)
    monkeypatch.setattr(worker, "dequeue_ahrefs_domain_rating_job", no_job)
    monkeypatch.setattr(worker, "dequeue_hypestat_fetch_job", no_job)
    monkeypatch.setattr(worker, "_find_recoverable_job", lambda: None)
    monkeypatch.setattr(worker, "_find_recoverable_pagespeed_job", lambda: None)
    monkeypatch.setattr(
        worker,
        "_find_recoverable_corpus_analysis_job",
        lambda: None,
    )
    monkeypatch.setattr(
        worker,
        "_find_recoverable_ahrefs_domain_rating_job",
        lambda: None,
    )
    monkeypatch.setattr(
        worker,
        "_find_recoverable_hypestat_fetch_job",
        lambda: None,
    )
    monkeypatch.setattr(worker, "process_due_gmail_monitoring", no_work)
    monkeypatch.setattr(worker, "process_due_campaign_recipient", no_work)
    monkeypatch.setattr(worker, "process_job", process_crawl_job)
    monkeypatch.setattr(worker.settings, "crawl_job_concurrency", concurrency)

    async def exercise_worker():
        task = asyncio.create_task(worker._run_worker_loop())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            assert maximum_jobs == concurrency
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise_worker())


def test_python_worker_can_leave_website_queues_to_go_worker(monkeypatch):
    queue_calls = []
    email_calls = []

    async def website_queue():
        queue_calls.append("redis")
        return None

    def website_recovery():
        queue_calls.append("database")
        return None

    async def no_work():
        email_calls.append("called")
        return False

    async def stop_after_first_poll(_delay):
        raise RuntimeError("stop worker test")

    monkeypatch.setattr(worker, "validate_redis_configuration", lambda: None)
    monkeypatch.setattr(worker, "dequeue_job", website_queue)
    monkeypatch.setattr(worker, "dequeue_pagespeed_job", website_queue)
    monkeypatch.setattr(worker, "dequeue_corpus_analysis_job", website_queue)
    monkeypatch.setattr(worker, "dequeue_ahrefs_domain_rating_job", website_queue)
    monkeypatch.setattr(worker, "dequeue_hypestat_fetch_job", website_queue)
    monkeypatch.setattr(worker, "_find_recoverable_job", website_recovery)
    monkeypatch.setattr(worker, "_find_recoverable_pagespeed_job", website_recovery)
    monkeypatch.setattr(worker, "_find_recoverable_corpus_analysis_job", website_recovery)
    monkeypatch.setattr(worker, "_find_recoverable_ahrefs_domain_rating_job", website_recovery)
    monkeypatch.setattr(worker, "_find_recoverable_hypestat_fetch_job", website_recovery)
    monkeypatch.setattr(worker, "process_due_gmail_monitoring", no_work)
    monkeypatch.setattr(worker, "process_due_campaign_recipient", no_work)
    monkeypatch.setattr(worker.asyncio, "sleep", stop_after_first_poll)
    monkeypatch.setattr(worker.settings, "python_worker_website_jobs_enabled", False)
    monkeypatch.setattr(worker.settings, "python_worker_campaign_sending_enabled", False)
    monkeypatch.setattr(worker.settings, "python_worker_gmail_monitoring_enabled", False)

    async def exercise_worker():
        with pytest.raises(RuntimeError, match="stop worker test"):
            await worker._run_worker_loop()

    asyncio.run(exercise_worker())
    assert queue_calls == []
    assert email_calls == []


def test_python_email_jobs_can_be_cut_over_independently(monkeypatch):
    calls = []

    async def no_job():
        return None

    async def process_campaign():
        calls.append("campaign")
        return False

    async def process_gmail():
        calls.append("gmail")
        return False

    async def stop_after_first_poll(_delay):
        raise RuntimeError("stop worker test")

    monkeypatch.setattr(worker, "validate_redis_configuration", lambda: None)
    monkeypatch.setattr(worker, "dequeue_job", no_job)
    monkeypatch.setattr(worker, "dequeue_pagespeed_job", no_job)
    monkeypatch.setattr(worker, "dequeue_corpus_analysis_job", no_job)
    monkeypatch.setattr(worker, "dequeue_ahrefs_domain_rating_job", no_job)
    monkeypatch.setattr(worker, "dequeue_hypestat_fetch_job", no_job)
    monkeypatch.setattr(worker, "_find_recoverable_job", lambda: None)
    monkeypatch.setattr(worker, "_find_recoverable_pagespeed_job", lambda: None)
    monkeypatch.setattr(worker, "_find_recoverable_corpus_analysis_job", lambda: None)
    monkeypatch.setattr(worker, "_find_recoverable_ahrefs_domain_rating_job", lambda: None)
    monkeypatch.setattr(worker, "_find_recoverable_hypestat_fetch_job", lambda: None)
    monkeypatch.setattr(worker, "process_due_gmail_monitoring", process_gmail)
    monkeypatch.setattr(worker, "process_due_campaign_recipient", process_campaign)
    monkeypatch.setattr(worker.asyncio, "sleep", stop_after_first_poll)
    monkeypatch.setattr(worker.settings, "python_worker_website_jobs_enabled", False)
    monkeypatch.setattr(worker.settings, "python_worker_campaign_sending_enabled", True)
    monkeypatch.setattr(worker.settings, "python_worker_gmail_monitoring_enabled", False)

    async def exercise_worker():
        with pytest.raises(RuntimeError, match="stop worker test"):
            await worker._run_worker_loop()

    asyncio.run(exercise_worker())
    assert calls == ["campaign"]
