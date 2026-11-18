import base64
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.models import (
    Company,
    ConnectedGmailAccount,
    Contact,
    EmailCampaign,
    EmailCampaignDelivery,
    EmailCampaignRecipient,
    EmailTemplate,
    SuppressedContact,
    UserGmailAssignment,
)
from app.services.campaign_monitor import _detect_hard_bounces, _detect_replies
from conftest import TestingSessionLocal


class _Request:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class _ThreadApi:
    def get(self, **kwargs):
        return _Request(
            {
                "messages": [
                    {
                        "internalDate": str(
                            int(datetime.now(timezone.utc).timestamp() * 1000)
                        ),
                        "payload": {
                            "headers": [
                                {
                                    "name": "From",
                                    "value": "Jane Doe <jane@example.test>",
                                }
                            ]
                        },
                    }
                ]
            }
        )


class _BounceMessageApi:
    def list(self, **kwargs):
        return _Request({"messages": [{"id": "bounce-message"}]})

    def get(self, **kwargs):
        body = (
            "Delivery Status Notification: delivery failed. "
            "Final-Recipient: jane@example.test. Status: 5.1.1 user unknown."
        )
        encoded = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
        return _Request(
            {
                "payload": {
                    "mimeType": "text/plain",
                    "body": {"data": encoded},
                }
            }
        )


class _UsersApi:
    def __init__(self, *, reply=False):
        self.reply = reply
        self.thread_api = _ThreadApi()
        self.message_api = _BounceMessageApi()

    def threads(self):
        return self.thread_api

    def messages(self):
        return self.message_api


class _FakeGmail:
    def __init__(self, *, reply=False):
        self.users_api = _UsersApi(reply=reply)

    def users(self):
        return self.users_api


def _create_monitor_target(client):
    session = client.get("/api/v1/auth/me").json()
    organization_id = UUID(session["organization_id"])
    user_id = UUID(session["user"]["id"])
    now = datetime.now(timezone.utc)
    with TestingSessionLocal() as db:
        company = Company(
            organization_id=organization_id,
            name=f"Monitoring {uuid4().hex[:8]}",
            domain="example.test",
        )
        db.add(company)
        db.flush()
        contact = Contact(
            organization_id=organization_id,
            company_id=company.id,
            name="Jane Doe",
            business_email="jane@example.test",
        )
        db.add(contact)
        account = ConnectedGmailAccount(
            org_id=organization_id,
            email_address="sender@example.test",
            google_account_id=f"monitor-{uuid4()}",
            encrypted_refresh_token="encrypted",
            token_updated_at=now,
        )
        db.add(account)
        db.flush()
        db.add(
            UserGmailAssignment(
                org_id=organization_id,
                user_id=user_id,
                gmail_account_id=account.id,
                assigned_by=user_id,
            )
        )
        template = EmailTemplate(
            org_id=organization_id,
            name="Monitor template",
            subject="Hello",
            body_content="Hello",
        )
        follow_up = EmailTemplate(
            org_id=organization_id,
            name="Monitor follow-up",
            subject="Following up",
            body_content="Following up",
        )
        db.add_all([template, follow_up])
        db.flush()
        campaign = EmailCampaign(
            org_id=organization_id,
            created_by=user_id,
            name="Monitor test",
            template_id=template.id,
            follow_up_steps=[
                {"template_id": str(follow_up.id), "delay_days": 3}
            ],
            gmail_account_ids=[str(account.id)],
            status="running",
        )
        db.add(campaign)
        db.flush()
        recipient = EmailCampaignRecipient(
            org_id=organization_id,
            campaign_id=campaign.id,
            company_id=company.id,
            contact_id=contact.id,
            gmail_account_id=account.id,
            to_email="jane@example.test",
            scheduled_at=now + timedelta(days=2),
            sent_at=now - timedelta(hours=1),
            gmail_thread_id="campaign-thread",
            current_step=1,
            sent_count=1,
            status="pending",
        )
        db.add(recipient)
        db.flush()
        db.add(
            EmailCampaignDelivery(
                org_id=organization_id,
                campaign_id=campaign.id,
                recipient_id=recipient.id,
                gmail_account_id=account.id,
                step_index=0,
                to_email="jane@example.test",
                gmail_message_id="campaign-message",
                gmail_thread_id="campaign-thread",
                sent_at=now - timedelta(hours=1),
            )
        )
        db.commit()
        return organization_id, account.id, recipient.id


def test_gmail_reply_marks_recipient_and_stops_follow_up(client):
    organization_id, account_id, recipient_id = _create_monitor_target(client)

    with TestingSessionLocal() as db:
        _detect_replies(
            db,
            _FakeGmail(reply=True),
            account_id,
            organization_id,
        )
        db.commit()

    with TestingSessionLocal() as db:
        recipient = db.get(EmailCampaignRecipient, recipient_id)
        campaign = db.get(EmailCampaign, recipient.campaign_id)
        assert recipient.status == "replied"
        assert recipient.replied_at is not None
        assert campaign.status == "completed"


def test_gmail_hard_bounce_suppresses_address_and_stops_follow_up(client):
    organization_id, account_id, recipient_id = _create_monitor_target(client)

    with TestingSessionLocal() as db:
        _detect_hard_bounces(
            db,
            _FakeGmail(),
            account_id,
            organization_id,
        )
        db.commit()

    with TestingSessionLocal() as db:
        recipient = db.get(EmailCampaignRecipient, recipient_id)
        suppression = db.query(SuppressedContact).filter_by(
            normalized_value="jane@example.test",
            reason="hard_bounce",
        ).one()
        assert recipient.status == "bounced"
        assert recipient.bounced_at is not None
        assert suppression.global_dnc is True
