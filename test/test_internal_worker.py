from uuid import uuid4


def test_internal_worker_job_dispatch_requires_token(client, monkeypatch):
    job_id = uuid4()
    processed = []

    async def fake_process(received_job_id):
        processed.append(received_job_id)

    worker_token = "worker-secret-long-enough-for-tests"
    monkeypatch.setattr("app.config.settings.worker_shared_token", worker_token)
    monkeypatch.setattr("app.worker.process_pagespeed_job", fake_process)

    denied = client.post(
        f"/internal/worker/jobs/pagespeed/{job_id}",
    )
    assert denied.status_code == 401
    assert processed == []

    response = client.post(
        f"/internal/worker/jobs/pagespeed/{job_id}",
        headers={"X-Worker-Token": worker_token},
    )
    assert response.status_code == 204
    assert processed == [job_id]


def test_internal_worker_recovery_and_scheduled_tasks_require_token(
    client,
    monkeypatch,
):
    recovered_id = uuid4()
    campaign_calls = []
    worker_token = "worker-secret-long-enough-for-tests"
    monkeypatch.setattr("app.config.settings.worker_shared_token", worker_token)

    def fake_recovery():
        return recovered_id

    async def fake_campaign():
        campaign_calls.append(True)
        return True

    monkeypatch.setattr("app.worker._find_recoverable_job", fake_recovery)
    monkeypatch.setattr(
        "app.services.campaign_worker.process_due_campaign_recipient",
        fake_campaign,
    )

    recovery = client.post(
        "/internal/worker/recover/crawl",
        headers={"X-Worker-Token": worker_token},
    )
    assert recovery.status_code == 200
    assert recovery.json() == {"job_id": str(recovered_id)}

    scheduled = client.post(
        "/internal/worker/scheduled/campaign",
        headers={"X-Worker-Token": worker_token},
    )
    assert scheduled.status_code == 200
    assert scheduled.json() == {"processed": True}
    assert campaign_calls == [True]
