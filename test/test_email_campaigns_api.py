from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.models import (
    ConnectedGmailAccount,
    Contact,
    EmailCampaign,
    EmailCampaignRecipient,
    EmailTemplate,
    UserGmailAssignment,
    Website,
)
from app.services import campaign_service
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


def _create_website_with_contact(organization_id, *, email="jane@example.test"):
    with TestingSessionLocal() as db:
        domain = f"example-{uuid4().hex}.test"
        website = Website(
            organization_id=organization_id,
            business_name=f"Example {uuid4().hex[:8]}",
            domain=domain,
            website_url=f"https://{domain}/",
            industry="Manufacturing",
        )
        db.add(website)
        db.flush()
        contact = Contact(
            organization_id=organization_id,
            website_id=website.id,
            name="Jane Doe",
            role="Operations Director",
            business_email=email,
        )
        db.add(contact)
        db.commit()
        return website.id, contact.id


def _create_website_without_valid_email(organization_id):
    with TestingSessionLocal() as db:
        website = Website(
            organization_id=organization_id,
            business_name=f"No email {uuid4().hex[:8]}",
            website_url="https://no-email.example.test/",
            domain="no-email.example.test",
        )
        db.add(website)
        db.flush()
        contact = Contact(
            organization_id=organization_id,
            website_id=website.id,
            name="No Email",
            business_email="not-an-email",
        )
        db.add(contact)
        db.commit()
        return website.id


def _create_template(client, organization_id):
    response = client.post(
        f"/api/v1/orgs/{organization_id}/templates",
        json={
            "name": "Campaign greeting",
            "subject": "Hello {{ contact.first_name | default('there') }}",
            "body_content": (
                "<p>Hello {{ contact.first_name | default('there') }}</p>"
                "<p>{{ sender.email }} · {{ website.domain }}</p>"
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
    website_contacts = [
        _create_website_with_contact(
            organization_id,
            email=f"person{index}@example.test",
        )
        for index in range(2)
    ]
    skipped_website_id = _create_website_without_valid_email(organization_id)
    website_ids = [item[0] for item in website_contacts] + [skipped_website_id]

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
        json={"website_ids": [str(website_id) for website_id in website_ids]},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible_contact_count"] == 2
    assert preview.json()["skipped_websites_without_email"] == 1
    assert preview.json()["skipped_website_ids"] == [str(skipped_website_id)]
    assert preview.json()["daily_capacity_per_day"] == 4
    assert preview.json()["estimated_completion_at"] is not None

    deleted_template = client.delete(
        f"/api/v1/orgs/{organization_id}/templates/{template_id}"
    )
    assert deleted_template.status_code == 409

    started = client.post(
        f"/api/v1/orgs/{organization_id}/campaigns/{campaign_id}/start",
        json={"website_ids": [str(website_id) for website_id in website_ids]},
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
        website_contacts[0][1]: account_ids[0],
        website_contacts[1][1]: account_ids[1],
    }


def test_campaign_preview_excludes_suppressed_contacts(client):
    organization_id, user_id = _session_ids(client)
    account_id = _create_account(organization_id, user_id)
    template_id = _create_template(client, organization_id)
    website_id, _ = _create_website_with_contact(
        organization_id,
        email="blocked@example.test",
    )
    created = _create_campaign(
        client,
        organization_id,
        template_id,
        [account_id],
    )
    assert created.status_code == 201, created.text

    suppressed = client.post(
        "/api/v1/email-suppressions",
        json={
            "target_type": "email",
            "value": "BLOCKED@EXAMPLE.TEST",
            "reason": "manual_dnc",
        },
    )
    assert suppressed.status_code == 201, suppressed.text

    preview = client.post(
        f"/api/v1/orgs/{organization_id}/campaigns/{created.json()['id']}/preview",
        json={"website_ids": [str(website_id)]},
    )

    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible_contact_count"] == 0
    assert preview.json()["skipped_suppressed_contacts"] == 1
    assert preview.json()["skipped_websites_without_email"] == 0
    assert preview.json()["skipped_website_ids"] == []


def test_campaign_accepts_configured_follow_up_steps(client):
    organization_id, user_id = _session_ids(client)
    account_id = _create_account(organization_id, user_id)
    first_template = _create_template(client, organization_id)
    follow_up_template = _create_template(client, organization_id)

    created = _create_campaign(
        client,
        organization_id,
        first_template,
        [account_id],
        follow_up_steps=[
            {"template_id": str(follow_up_template), "delay_days": 4}
        ],
        verify_mx=False,
    )

    assert created.status_code == 201, created.text
    assert created.json()["follow_up_steps"] == [
        {"template_id": str(follow_up_template), "delay_days": 4}
    ]
    assert created.json()["verify_mx"] is False


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
    website_contacts = [
        _create_website_with_contact(
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
            "website_ids": [
                str(website_id) for website_id, _ in website_contacts
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


def test_campaign_api_rejects_websites_outside_organization(client):
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
        json={"website_ids": [str(uuid4())]},
    )
    assert preview.status_code == 404


def test_paused_campaign_delete_removes_pending_recipients(client):
    organization_id, user_id = _session_ids(client)
    account_id = _create_account(organization_id, user_id)
    template_id = _create_template(client, organization_id)
    website_id, _ = _create_website_with_contact(organization_id)
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
        json={"website_ids": [str(website_id)]},
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
