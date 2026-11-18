import base64
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.models import (
    ConnectedGmailAccount,
    Organization,
    SuppressedContact,
    UserGmailAssignment,
)
from app.schemas.gmail import GmailComposeInput
from app.services import email_suppression, gmail
from conftest import TestingSessionLocal


def _session_ids(client):
    session = client.get("/api/v1/auth/me").json()
    return UUID(session["organization_id"]), UUID(session["user"]["id"])


def _create_gmail_account(organization_id: UUID, user_id: UUID) -> UUID:
    with TestingSessionLocal() as db:
        account = ConnectedGmailAccount(
            org_id=organization_id,
            email_address="sender@example.test",
            google_account_id=f"google-{uuid4()}",
            encrypted_refresh_token="encrypted-refresh-token",
            token_updated_at=datetime.now(timezone.utc),
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
        db.commit()
        return account.id


def test_unsubscribe_is_public_signed_and_idempotent(client):
    organization_id, _ = _session_ids(client)
    token = email_suppression.create_unsubscribe_token(
        organization_id,
        "Person@Example.test",
    )

    client.cookies.clear()
    assert client.get("/api/v1/unsubscribe", params={"token": token}).status_code == 200
    response = client.post("/api/v1/unsubscribe", json={"token": token})
    assert response.status_code == 200, response.text
    repeated = client.post("/api/v1/unsubscribe", json={"token": token})
    assert repeated.status_code == 200, repeated.text
    one_click_token = email_suppression.create_unsubscribe_token(
        organization_id,
        "oneclick@example.test",
    )
    one_click = client.post(
        "/api/v1/unsubscribe/one-click",
        params={"token": one_click_token},
        content="List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert one_click.status_code == 204

    with TestingSessionLocal() as db:
        rows = db.scalars(
            select(SuppressedContact).where(
                SuppressedContact.organization_id == organization_id,
                SuppressedContact.target_type == "email",
                SuppressedContact.normalized_value == "person@example.test",
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].reason == "unsubscribe"
    assert rows[0].global_dnc is True
    with TestingSessionLocal() as db:
        one_click_record = db.scalar(
            select(SuppressedContact).where(
                SuppressedContact.organization_id == organization_id,
                SuppressedContact.normalized_value == "oneclick@example.test",
            )
        )
    assert one_click_record is not None


def test_unsubscribe_rejects_tampered_token(client):
    organization_id, _ = _session_ids(client)
    token = email_suppression.create_unsubscribe_token(
        organization_id,
        "person@example.test",
    )
    version, payload, signature = token.split(".")
    replacement = "A" if payload[0] != "A" else "B"

    response = client.post(
        "/api/v1/unsubscribe",
        json={"token": f"{version}.{replacement}{payload[1:]}.{signature}"},
    )

    assert response.status_code == 400


def test_domain_suppression_matches_subdomains_across_organizations(client):
    organization_id, _ = _session_ids(client)
    with TestingSessionLocal() as db:
        other_organization = Organization(name="Another organization")
        db.add(other_organization)
        db.commit()
        other_organization_id = other_organization.id
        email_suppression.add_suppression(
            db,
            organization_id=organization_id,
            target_type="domain",
            value="example.test",
            reason="manual_dnc",
        )
        db.commit()

        assert email_suppression.email_suppression_reasons(
            db,
            organization_id,
            ["person@sub.example.test", "person@notexample.test"],
        ) == {"person@sub.example.test": "manual_dnc"}
        assert email_suppression.email_suppression_reasons(
            db,
            other_organization_id,
            ["person@sub.example.test"],
        ) == {"person@sub.example.test": "manual_dnc"}


def test_campaign_and_direct_sends_are_blocked_for_suppressed_recipient(
    client,
    monkeypatch,
):
    organization_id, user_id = _session_ids(client)
    account_id = _create_gmail_account(organization_id, user_id)
    add_response = client.post(
        "/api/v1/email-suppressions",
        json={
            "target_type": "email",
            "value": "blocked@example.test",
            "reason": "manual_dnc",
        },
    )
    assert add_response.status_code == 201, add_response.text

    def unexpected_gmail_service(_account):
        raise AssertionError("Gmail must not be contacted for a suppressed address.")

    monkeypatch.setattr(gmail, "_build_gmail_service", unexpected_gmail_service)
    with TestingSessionLocal() as db:
        with pytest.raises(HTTPException) as error:
            gmail.send_gmail_message(
                db,
                account_id,
                recipient_email="blocked@example.test",
                subject="Hello",
                body_content="<p>Hello</p>",
            )
    assert error.value.status_code == 409


@pytest.mark.parametrize("send_path", ["compose", "reply"])
def test_gmail_compose_and_reply_block_suppressed_recipients(
    client,
    monkeypatch,
    send_path,
):
    organization_id, user_id = _session_ids(client)
    account_id = _create_gmail_account(organization_id, user_id)
    created = client.post(
        "/api/v1/email-suppressions",
        json={
            "target_type": "email",
            "value": "blocked@example.test",
            "reason": "unsubscribe",
        },
    )
    assert created.status_code == 201

    class FakeRequest:
        def execute(self):
            return {
                "threadId": "thread-id",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "blocked@example.test"},
                        {"name": "Subject", "value": "Original message"},
                    ]
                },
            }

    class FakeMessages:
        def get(self, **_parameters):
            return FakeRequest()

        def send(self, **_parameters):
            raise AssertionError("A suppressed recipient must never be sent email.")

    class FakeUsers:
        def messages(self):
            return FakeMessages()

    class FakeService:
        def users(self):
            return FakeUsers()

    monkeypatch.setattr(gmail, "_build_gmail_service", lambda _account: FakeService())
    with TestingSessionLocal() as db:
        with pytest.raises(HTTPException) as error:
            if send_path == "compose":
                gmail.compose_gmail_message(
                    db,
                    account_id,
                    GmailComposeInput(
                        to=["blocked@example.test"],
                        subject="Hello",
                        body_text="Hello",
                    ),
                )
            else:
                gmail.reply_to_gmail_message(
                    db,
                    account_id,
                    "original-message",
                    "Hello",
                )

    assert error.value.status_code == 409


def test_gmail_message_has_footer_and_one_click_headers(client, monkeypatch):
    organization_id, user_id = _session_ids(client)
    account_id = _create_gmail_account(organization_id, user_id)
    monkeypatch.setattr(settings, "frontend_origins", "https://app.example.test")
    sent_body = {}

    class FakeRequest:
        def execute(self):
            return {"id": "message-id", "threadId": "thread-id"}

    class FakeMessages:
        def send(self, *, userId, body):
            assert userId == "me"
            sent_body.update(body)
            return FakeRequest()

    class FakeUsers:
        def messages(self):
            return FakeMessages()

    class FakeService:
        def users(self):
            return FakeUsers()

    monkeypatch.setattr(gmail, "_build_gmail_service", lambda _account: FakeService())
    with TestingSessionLocal() as db:
        gmail.send_gmail_message(
            db,
            account_id,
            recipient_email="person@example.test",
            subject="Hello",
            body_content="<p>Hello there</p>",
        )

    raw_message = base64.urlsafe_b64decode(
        sent_body["raw"] + "=" * (-len(sent_body["raw"]) % 4)
    )
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    assert "List-Unsubscribe-Post" in message
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "/unsubscribe/one-click?token=" in message["List-Unsubscribe"]
    plain_body = message.get_body(preferencelist=("plain",)).get_content()
    html_body = message.get_body(preferencelist=("html",)).get_content()
    assert "To stop receiving emails" in plain_body
    assert "Unsubscribe" in html_body

    with TestingSessionLocal() as db:
        gmail.compose_gmail_message(
            db,
            account_id,
            GmailComposeInput(
                to=["person@example.test"],
                bcc=["hidden@example.test"],
                subject="Hello",
                body_text="Hello there",
            ),
        )
    multi_recipient = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(
            sent_body["raw"] + "=" * (-len(sent_body["raw"]) % 4)
        )
    )
    generic_url = multi_recipient["List-Unsubscribe"].strip("<>")
    claims = email_suppression.verify_unsubscribe_token(
        parse_qs(urlsplit(generic_url).query)["token"][0]
    )
    assert claims.email is None
    generic_token = parse_qs(urlsplit(generic_url).query)["token"][0]
    verified = client.get(
        "/api/v1/unsubscribe",
        params={"token": generic_token},
    )
    assert verified.status_code == 200
    assert verified.json()["requires_email"] is True
    wrong_address = client.post(
        "/api/v1/unsubscribe",
        json={"token": generic_token, "email": "other@example.test"},
    )
    assert wrong_address.status_code == 400
    accepted_address = client.post(
        "/api/v1/unsubscribe",
        json={"token": generic_token, "email": "hidden@example.test"},
    )
    assert accepted_address.status_code == 200
    assert "hidden@example.test" in multi_recipient["Bcc"]
    assert "hidden@example.test" not in generic_url
    assert "List-Unsubscribe-Post" not in multi_recipient
    assert "hidden@example.test" not in multi_recipient.get_content()
