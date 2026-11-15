import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.models import (
    Company,
    ConnectedGmailAccount,
    Contact,
    EmailCampaign,
    EmailCampaignRecipient,
    EmailTemplate,
    UserGmailAssignment,
)
from app.services import campaign_service, campaign_worker
from app.services.gmail import GmailSendResult
from conftest import TestingSessionLocal


def _session_ids(client):
    session = client.get("/api/v1/auth/me").json()
    return UUID(session["organization_id"]), UUID(session["user"]["id"])


def _create_account(organization_id, user_id, *, assigned=True):
    with TestingSessionLocal() as db:
        account = ConnectedGmailAccount(
            org_id=organization_id,
            email_address=f"sender-{uuid4().hex[:8]}@example.test",
            google_account_id=f"google-{uuid4()}",
            encrypted_refresh_token="encrypted-refresh-token",
            token_updated_at=datetime.now(timezone.utc),
        )
        db.add(account)
        db.flush()
        if assigned:
            db.add(
                UserGmailAssignment(
                    org_id=organization_id,
                    user_id=user_id,
                    gmail_account_id=account.id,
                    assigned_by=user_id,
                )
            )
        db.commit()
        return account.id


def _create_company_with_contact(organization_id, *, email="jane@example.test"):
    with TestingSessionLocal() as db:
        domain = f"example-{uuid4().hex}.test"
        company = Company(
            organization_id=organization_id,
            name=f"Example {uuid4().hex[:8]}",
            domain=domain,
            website=f"https://{domain}",
            industry="Manufacturing",
        )
        db.add(company)
        db.flush()
        contact = Contact(
            organization_id=organization_id,
            company_id=company.id,
            name="Jane Doe",
            role="Operations Director",
            business_email=email,
        )
        db.add(contact)
        db.commit()
        return company.id, contact.id


def _create_company_without_valid_email(organization_id):
    with TestingSessionLocal() as db:
        company = Company(
            organization_id=organization_id,
            name=f"No email {uuid4().hex[:8]}",
            domain="no-email.example.test",
        )
        db.add(company)
        db.flush()
        contact = Contact(
            organization_id=organization_id,
            company_id=company.id,
            name="No Email",
            business_email="not-an-email",
        )
        db.add(contact)
        db.commit()
        return company.id


def _create_template(client, organization_id):
    response = client.post(
        f"/api/v1/orgs/{organization_id}/templates",
        json={
            "name": "Campaign greeting",
            "subject": "Hello {{ contact.first_name | default('there') }}",
            "body_content": (
                "<p>Hello {{ contact.first_name | default('there') }}</p>"
                "<p>{{ sender.email }} · {{ company.domain }}</p>"
            ),
        },
    )
    assert response.status_code == 201
    return UUID(response.json()["id"])


def _create_campaign(
    client,
    organization_id,
    template_id,
    account_ids,
    **overrides,
):
    payload = {
        "name": "April follow-up",
        "template_id": str(template_id),
        "gmail_account_ids": [str(account_id) for account_id in account_ids],
        "daily_limit_per_account": 40,
        "min_delay_seconds": 180,
        "max_delay_seconds": 180,
        "send_window_start_hour": 0,
        "send_window_end_hour": 23,
        "timezone": "UTC",
    }
    payload.update(overrides)
    response = client.post(
        f"/api/v1/orgs/{organization_id}/campaigns",
        json=payload,
    )
    return response


def test_campaign_preview_and_start_round_robin_accounts(client):
    organization_id, user_id = _session_ids(client)
    account_ids = [
        _create_account(organization_id, user_id),
        _create_account(organization_id, user_id),
    ]
    template_id = _create_template(client, organization_id)
    company_contacts = [
        _create_company_with_contact(
            organization_id,
            email=f"person{index}@example.test",
        )
        for index in range(2)
    ]
    skipped_company_id = _create_company_without_valid_email(organization_id)
    company_ids = [item[0] for item in company_contacts] + [skipped_company_id]

    created = _create_campaign(
        client,
        organization_id,
        template_id,
        account_ids,
        daily_limit_per_account=2,
    )
    assert created.status_code == 201, created.text
    campaign_id = created.json()["id"]

    preview = client.post(
        f"/api/v1/orgs/{organization_id}/campaigns/{campaign_id}/preview",
        json={"company_ids": [str(company_id) for company_id in company_ids]},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible_contact_count"] == 2
    assert preview.json()["skipped_companies_without_email"] == 1
    assert preview.json()["skipped_company_ids"] == [str(skipped_company_id)]
    assert preview.json()["daily_capacity_per_day"] == 4
    assert preview.json()["estimated_completion_at"] is not None

    deleted_template = client.delete(
        f"/api/v1/orgs/{organization_id}/templates/{template_id}"
    )
    assert deleted_template.status_code == 409

    started = client.post(
        f"/api/v1/orgs/{organization_id}/campaigns/{campaign_id}/start",
        json={"company_ids": [str(company_id) for company_id in company_ids]},
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "running"
    assert started.json()["recipient_counts"]["pending"] == 2
    detail = client.get(
        f"/api/v1/orgs/{organization_id}/campaigns/{campaign_id}"
    )
    assert detail.status_code == 200
    assert detail.json()["recipient_counts"]["pending"] == 2
    listed = client.get(f"/api/v1/orgs/{organization_id}/campaigns")
    assert listed.status_code == 200
    assert any(item["id"] == campaign_id for item in listed.json())

    recipient_accounts = {}
    with TestingSessionLocal() as db:
        for recipient in db.query(EmailCampaignRecipient).filter_by(
            campaign_id=UUID(campaign_id)
        ):
            recipient_accounts[recipient.contact_id] = recipient.gmail_account_id

    assert recipient_accounts == {
        company_contacts[0][1]: account_ids[0],
        company_contacts[1][1]: account_ids[1],
    }


def test_campaign_gmail_assignment_is_required(client):
    organization_id, user_id = _session_ids(client)
    unassigned_account = _create_account(
        organization_id,
        user_id,
        assigned=False,
    )
    template_id = _create_template(client, organization_id)

    response = _create_campaign(
        client,
        organization_id,
        template_id,
        [unassigned_account],
    )
    assert response.status_code == 403
    assert client.get(
        f"/api/v1/orgs/{uuid4()}/campaigns"
    ).status_code == 404


def test_campaign_pause_and_resume_recalculate_pending_schedule(
    client,
    monkeypatch,
):
    organization_id, user_id = _session_ids(client)
    account_id = _create_account(organization_id, user_id)
    template_id = _create_template(client, organization_id)
    company_contacts = [
        _create_company_with_contact(
            organization_id,
            email=f"person{index}@example.test",
        )
        for index in range(3)
    ]
    created = _create_campaign(
        client,
        organization_id,
        template_id,
        [account_id],
        daily_limit_per_account=2,
        send_window_start_hour=9,
        send_window_end_hour=17,
    )
    assert created.status_code == 201, created.text
    campaign_id = created.json()["id"]
    first_clock = datetime(2025, 1, 6, 9, 0, tzinfo=timezone.utc)
    now = [first_clock]
    monkeypatch.setattr(campaign_service, "utc_now", lambda: now[0])

    started = client.post(
        f"/api/v1/orgs/{organization_id}/campaigns/{campaign_id}/start",
        json={
            "company_ids": [
                str(company_id) for company_id, _ in company_contacts
            ]
        },
    )
    assert started.status_code == 200, started.text

    with TestingSessionLocal() as db:
        initial_schedules = {
            recipient.contact_id: recipient.scheduled_at
            for recipient in db.query(EmailCampaignRecipient).filter_by(
                campaign_id=UUID(campaign_id)
            )
        }
    assert len(initial_schedules) == 3

    paused = client.post(
        f"/api/v1/orgs/{organization_id}/campaigns/{campaign_id}/pause"
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"

    resume_time = datetime(2025, 1, 7, 10, 0, tzinfo=timezone.utc)
    now[0] = resume_time
    resumed = client.post(
        f"/api/v1/orgs/{organization_id}/campaigns/{campaign_id}/resume"
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "running"

    with TestingSessionLocal() as db:
        rescheduled = {
            recipient.contact_id: recipient.scheduled_at
            for recipient in db.query(EmailCampaignRecipient).filter_by(
                campaign_id=UUID(campaign_id)
            )
        }
    assert all(
        scheduled.replace(tzinfo=timezone.utc) >= resume_time
        for scheduled in rescheduled.values()
    )
    assert rescheduled != initial_schedules


def test_campaign_worker_sends_and_completes_campaign(client, monkeypatch):
    organization_id, user_id = _session_ids(client)
    account_id = _create_account(organization_id, user_id)
    template_id = _create_template(client, organization_id)
    company_id, contact_id = _create_company_with_contact(organization_id)

    with TestingSessionLocal() as db:
        campaign = EmailCampaign(
            org_id=organization_id,
            created_by=user_id,
            name="Worker test",
            template_id=template_id,
            gmail_account_ids=[str(account_id)],
            daily_limit_per_account=40,
            min_delay_seconds=180,
            max_delay_seconds=420,
            send_window_start_hour=9,
            send_window_end_hour=17,
            timezone="UTC",
            status="running",
        )
        db.add(campaign)
        db.flush()
        recipient = EmailCampaignRecipient(
            org_id=organization_id,
            campaign_id=campaign.id,
            company_id=company_id,
            contact_id=contact_id,
            gmail_account_id=account_id,
            to_email="jane@example.test",
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            status="pending",
        )
        db.add(recipient)
        db.commit()
        campaign_id = campaign.id
        recipient_id = recipient.id

    captured = {}
    with TestingSessionLocal() as db:
        sender_email = db.get(
            ConnectedGmailAccount,
            account_id,
        ).email_address

    def fake_send(db, gmail_account_id, *, recipient_email, subject, body_content):
        captured.update(
            account_id=gmail_account_id,
            recipient_email=recipient_email,
            subject=subject,
            body_content=body_content,
        )
        return GmailSendResult(
            message_id="campaign-message-id",
            thread_id="campaign-thread-id",
            sender_email="sender@example.test",
        )

    monkeypatch.setattr(campaign_worker, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(campaign_worker, "send_gmail_message", fake_send)

    assert asyncio.run(campaign_worker.process_due_campaign_recipient()) is True
    assert captured["account_id"] == account_id
    assert captured["recipient_email"] == "jane@example.test"
    assert captured["subject"] == "Hello Jane"
    assert f"{sender_email} · " in captured["body_content"]

    with TestingSessionLocal() as db:
        sent_recipient = db.get(EmailCampaignRecipient, recipient_id)
        saved_campaign = db.get(EmailCampaign, campaign_id)
        assert sent_recipient.status == "sent"
        assert sent_recipient.gmail_message_id == "campaign-message-id"
        assert sent_recipient.gmail_thread_id == "campaign-thread-id"
        assert saved_campaign.status == "completed"


def test_scheduler_respects_business_hours_weekends_and_daily_cap(monkeypatch):
    campaign = EmailCampaign(
        timezone="UTC",
        send_window_start_hour=9,
        send_window_end_hour=10,
        min_delay_seconds=1800,
        max_delay_seconds=1800,
        daily_limit_per_account=1,
    )
    account_id = uuid4()
    start = datetime(2025, 1, 10, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(campaign_service, "randint", lambda _minimum, maximum: maximum)

    slots = campaign_service.schedule_account_assignments(
        [account_id] * 4,
        campaign,
        start_at=start,
    )

    assert [scheduled for _, scheduled in slots] == [
        datetime(2025, 1, 10, 9, 30, tzinfo=timezone.utc),
        datetime(2025, 1, 13, 9, 0, tzinfo=timezone.utc),
        datetime(2025, 1, 14, 9, 0, tzinfo=timezone.utc),
        datetime(2025, 1, 15, 9, 0, tzinfo=timezone.utc),
    ]


def test_campaign_api_rejects_companies_outside_organization(client):
    organization_id, user_id = _session_ids(client)
    account_id = _create_account(organization_id, user_id)
    template_id = _create_template(client, organization_id)
    campaign_response = _create_campaign(
        client,
        organization_id,
        template_id,
        [account_id],
    )
    assert campaign_response.status_code == 201, campaign_response.text

    campaign_id = campaign_response.json()["id"]
    preview = client.post(
        f"/api/v1/orgs/{organization_id}/campaigns/{campaign_id}/preview",
        json={"company_ids": [str(uuid4())]},
    )
    assert preview.status_code == 404


def test_paused_campaign_delete_removes_pending_recipients(client):
    organization_id, user_id = _session_ids(client)
    account_id = _create_account(organization_id, user_id)
    template_id = _create_template(client, organization_id)
    company_id, _ = _create_company_with_contact(organization_id)
    created = _create_campaign(
        client,
        organization_id,
        template_id,
        [account_id],
    )
    assert created.status_code == 201, created.text
    campaign_id = created.json()["id"]

    started = client.post(
        f"/api/v1/orgs/{organization_id}/campaigns/{campaign_id}/start",
        json={"company_ids": [str(company_id)]},
    )
    assert started.status_code == 200, started.text
    paused = client.post(
        f"/api/v1/orgs/{organization_id}/campaigns/{campaign_id}/pause"
    )
    assert paused.status_code == 200, paused.text

    deleted = client.delete(
        f"/api/v1/orgs/{organization_id}/campaigns/{campaign_id}"
    )
    assert deleted.status_code == 204
    assert client.get(
        f"/api/v1/orgs/{organization_id}/campaigns/{campaign_id}"
    ).status_code == 404
    with TestingSessionLocal() as db:
        assert db.query(EmailCampaignRecipient).filter_by(
            campaign_id=UUID(campaign_id)
        ).count() == 0
