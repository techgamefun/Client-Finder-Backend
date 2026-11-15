import base64
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main
from app.api import gmail as gmail_api
from app.config import settings
from app.models import ConnectedGmailAccount
from app.services import gmail, gmail_oauth
from app.tenant_context import TenantContext
from conftest import TestingSessionLocal


def test_gmail_body_decoder_preserves_unpadded_base64_tail():
    encoded = base64.urlsafe_b64encode(b"Hi").decode("ascii").rstrip("=")

    assert gmail._decode_body_data(encoded) == "Hi"


def _configure_google_oauth(monkeypatch):
    monkeypatch.setattr(settings, "frontend_origins", "http://localhost:3000")
    monkeypatch.setattr(
        settings,
        "google_oauth_client_id",
        "gmail-client-id",
    )
    monkeypatch.setattr(
        settings,
        "google_oauth_client_secret",
        "gmail-client-secret",
    )
    monkeypatch.setattr(
        settings,
        "google_oauth_redirect_uri",
        "http://localhost:8000/api/v1/gmail/callback",
    )
    monkeypatch.setattr(settings, "google_oauth_state_secret", "33" * 32)
    monkeypatch.setattr(
        settings,
        "gmail_token_encryption_key",
        Fernet.generate_key().decode("ascii"),
    )


def test_gmail_oauth_connect_and_admin_assignment(client, monkeypatch):
    _configure_google_oauth(monkeypatch)
    session = client.get("/api/v1/auth/me").json()
    organization_id = UUID(session["organization_id"])
    admin_user_id = UUID(session["user"]["id"])

    start = client.get(
        "/api/v1/auth/google/login",
        params={
            "org_id": str(organization_id),
            "admin_user_id": str(admin_user_id),
        },
        headers={"referer": "http://localhost:3000/settings"},
        follow_redirects=False,
    )
    assert start.status_code == 302
    google_query = parse_qs(urlsplit(start.headers["location"]).query)
    state = google_query["state"][0]
    decoded_state = gmail_oauth.decode_google_oauth_state(state)
    assert decoded_state.org_id == organization_id
    assert decoded_state.admin_user_id == admin_user_id
    assert google_query["access_type"] == ["offline"]
    assert "https://www.googleapis.com/auth/gmail.modify" in google_query["scope"][0]
    assert "https://www.googleapis.com/auth/gmail.send" in google_query["scope"][0]

    with pytest.raises(HTTPException) as invalid_state:
        gmail_oauth.decode_google_oauth_state(state + "tampered")
    assert invalid_state.value.status_code == 400

    async def exchange_code(code: str):
        assert code == "authorization-code"
        return gmail_oauth.GoogleOAuthTokens(
            access_token="test-access-token",
            refresh_token="test-refresh-token",
        )

    async def fetch_profile(access_token: str):
        assert access_token == "test-access-token"
        return gmail_oauth.GoogleProfile(
            google_account_id="google-user-123",
            email_address="sender@example.com",
        )

    monkeypatch.setattr(
        gmail_api,
        "exchange_google_authorization_code",
        exchange_code,
    )
    monkeypatch.setattr(
        gmail_api,
        "fetch_google_profile",
        fetch_profile,
    )
    callback = client.get(
        "/api/v1/gmail/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == (
        "http://localhost:3000/settings?gmail=connected"
    )

    async def exchange_without_refresh_token(code: str):
        assert code == "reauthorization-code"
        return gmail_oauth.GoogleOAuthTokens(
            access_token="test-access-token",
            refresh_token=None,
        )

    monkeypatch.setattr(
        gmail_api,
        "exchange_google_authorization_code",
        exchange_without_refresh_token,
    )
    reconnect = client.get(
        "/api/v1/auth/google/login",
        params={
            "org_id": str(organization_id),
            "admin_user_id": str(admin_user_id),
        },
        headers={"referer": "http://localhost:3000/settings"},
        follow_redirects=False,
    )
    reconnect_state = parse_qs(
        urlsplit(reconnect.headers["location"]).query
    )["state"][0]
    reconnected = client.get(
        "/api/v1/gmail/callback",
        params={"code": "reauthorization-code", "state": reconnect_state},
        follow_redirects=False,
    )
    assert reconnected.status_code == 303

    with TestingSessionLocal() as db:
        account = db.scalar(
            select(ConnectedGmailAccount).where(
                ConnectedGmailAccount.google_account_id == "google-user-123"
            )
        )
        assert account is not None
        assert account.email_address == "sender@example.com"
        assert account.encrypted_refresh_token != "test-refresh-token"
        assert gmail.decrypt_refresh_token(account.encrypted_refresh_token) == (
            "test-refresh-token"
        )
        gmail_account_id = account.id

    created_user = client.post(
        "/api/v1/auth/users",
        json={
            "email": "gmail-user@example.test",
            "password": "gmail-user-password-long",
            "role": "user",
        },
    )
    assert created_user.status_code == 201
    user_id = created_user.json()["id"]
    assignment = client.post(
        f"/api/v1/gmail/accounts/{gmail_account_id}/assignments",
        json={"user_id": user_id},
    )
    assert assignment.status_code == 201
    assert assignment.json()["assigned_by"] == str(admin_user_id)
    assert assignment.json()["org_id"] == str(organization_id)
    assert client.post(
        f"/api/v1/gmail/accounts/{gmail_account_id}/assignments",
        json={"user_id": user_id},
    ).status_code == 409

    built_service = object()
    captured = {}

    def fake_build(service_name, version, *, credentials, cache_discovery):
        captured["service_name"] = service_name
        captured["version"] = version
        captured["credentials"] = credentials
        captured["cache_discovery"] = cache_discovery
        return built_service

    monkeypatch.setattr(gmail, "build", fake_build)
    with TestingSessionLocal() as db:
        with pytest.raises(HTTPException) as unassigned:
            gmail.get_gmail_service(db, gmail_account_id)
        assert unassigned.value.status_code == 403
        db.info["tenant_context"] = TenantContext(
            organization_id=organization_id,
            user_id=UUID(user_id),
            email="gmail-user@example.test",
            role="user",
        )
        service = gmail.get_gmail_service(db, gmail_account_id)
    assert service is built_service
    assert captured["service_name"] == "gmail"
    assert captured["version"] == "v1"
    assert captured["cache_discovery"] is False
    assert captured["credentials"].refresh_token == "test-refresh-token"

    api_calls = {}
    html_body = (
        "<p>Hello <script>ignore-this</script><b>there</b></p>"
    )
    encoded_body = base64.urlsafe_b64encode(
        html_body.encode("utf-8")
    ).decode("ascii").rstrip("=")
    message_metadata = {
        "id": "gmail-message-1",
        "threadId": "gmail-thread-1",
        "snippet": "Hello there",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "headers": [
                {"name": "From", "value": "Sender <sender@example.com>"},
                {"name": "Reply-To", "value": "reply@example.com"},
                {"name": "Subject", "value": "A question"},
                {"name": "Date", "value": "Mon, 16 Nov 2026 10:00:00 +0000"},
                {"name": "Message-ID", "value": "<original@example.com>"},
                {"name": "References", "value": "<earlier@example.com>"},
            ]
        },
    }

    class FakeRequest:
        def __init__(self, response):
            self.response = response

        def execute(self):
            return self.response

    class FakeMessages:
        def list(self, **parameters):
            api_calls["list_parameters"] = parameters
            return FakeRequest(
                {
                    "messages": [
                        {
                            "id": "gmail-message-1",
                            "threadId": "gmail-thread-1",
                            "snippet": "Hello there",
                            "labelIds": ["INBOX", "UNREAD"],
                        }
                    ],
                    "resultSizeEstimate": 1,
                }
            )

        def get(self, **parameters):
            if parameters["format"] == "metadata":
                return FakeRequest(message_metadata)
            return FakeRequest(
                {
                    **message_metadata,
                    "payload": {
                        **message_metadata["payload"],
                        "mimeType": "text/html",
                        "body": {"data": encoded_body},
                    },
                }
            )

        def send(self, *, userId, body):
            del userId
            api_calls["thread_id"] = body["threadId"]
            raw = body["raw"]
            raw += "=" * (-len(raw) % 4)
            sent_message = BytesParser(policy=policy.default).parsebytes(
                base64.urlsafe_b64decode(raw)
            )
            api_calls["sent_to"] = sent_message["To"]
            api_calls["sent_subject"] = sent_message["Subject"]
            api_calls["sent_in_reply_to"] = sent_message["In-Reply-To"]
            api_calls["sent_body"] = sent_message.get_content()
            return FakeRequest(
                {"id": "sent-message-1", "threadId": "gmail-thread-1"}
            )

    class FakeUsers:
        def messages(self):
            return FakeMessages()

    class FakeService:
        def users(self):
            return FakeUsers()

    monkeypatch.setattr(gmail, "build", lambda *_args, **_kwargs: FakeService())
    assert client.get("/api/v1/gmail/accounts/assigned").json() == []
    denied_inbox = client.get(
        f"/api/v1/gmail/accounts/{gmail_account_id}/messages"
    )
    assert denied_inbox.status_code == 403

    with TestClient(main.app) as member_client:
        login = member_client.post(
            "/api/v1/auth/login",
            json={
                "email": "gmail-user@example.test",
                "password": "gmail-user-password-long",
            },
        )
        assert login.status_code == 200
        assigned_accounts = member_client.get("/api/v1/gmail/accounts/assigned")
        assert assigned_accounts.status_code == 200
        assert assigned_accounts.json()[0]["id"] == str(gmail_account_id)

        inbox = member_client.get(
            f"/api/v1/gmail/accounts/{gmail_account_id}/messages",
            params={"q": "from:sender@example.com", "max_results": 5},
        )
        assert inbox.status_code == 200
        assert inbox.json()["messages"][0]["is_unread"] is True
        assert api_calls["list_parameters"]["q"] == "from:sender@example.com"

        detail = member_client.get(
            f"/api/v1/gmail/accounts/{gmail_account_id}/messages/gmail-message-1"
        )
        assert detail.status_code == 200
        assert detail.json()["body_text"] == "Hello there"
        assert detail.json()["is_unread"] is True
        assert "ignore-this" not in detail.json()["body_text"]

        sent = member_client.post(
            f"/api/v1/gmail/accounts/{gmail_account_id}/messages/gmail-message-1/reply",
            json={"body_text": "Thanks for reaching out."},
        )
        assert sent.status_code == 200
        assert sent.json()["thread_id"] == "gmail-thread-1"
        assert api_calls["thread_id"] == "gmail-thread-1"
        assert api_calls["sent_to"] == "reply@example.com"
        assert api_calls["sent_subject"] == "Re: A question"
        assert api_calls["sent_in_reply_to"] == "<original@example.com>"
        assert api_calls["sent_body"] == "Thanks for reaching out.\r\n"

    accounts = client.get("/api/v1/gmail/accounts")
    assert accounts.status_code == 200
    assert [row["email_address"] for row in accounts.json()] == [
        "sender@example.com"
    ]
    assert accounts.json()[0]["assigned_users"][0]["email"] == (
        "gmail-user@example.test"
    )
    assert "encrypted_refresh_token" not in accounts.text

    removed = client.delete(
        f"/api/v1/gmail/assignments/{assignment.json()['id']}"
    )
    assert removed.status_code == 204
    refreshed_accounts = client.get("/api/v1/gmail/accounts")
    assert refreshed_accounts.json()[0]["assigned_users"] == []


def test_gmail_oauth_and_assignment_require_admin(client, monkeypatch):
    _configure_google_oauth(monkeypatch)
    admin_session = client.get("/api/v1/auth/me").json()
    account_id = uuid4()
    user = client.post(
        "/api/v1/auth/users",
        json={
            "email": "gmail-member@example.test",
            "password": "gmail-member-password-long",
            "role": "user",
        },
    )
    assert user.status_code == 201

    with TestClient(main.app) as member_client:
        login = member_client.post(
            "/api/v1/auth/login",
            json={
                "email": "gmail-member@example.test",
                "password": "gmail-member-password-long",
            },
        )
        assert login.status_code == 200
        oauth_start = member_client.get(
            "/api/v1/auth/google/login",
            params={
                "org_id": admin_session["organization_id"],
                "admin_user_id": user.json()["id"],
            },
            headers={"referer": "http://localhost:3000/settings"},
            follow_redirects=False,
        )
        assert oauth_start.status_code == 403
        assignment = member_client.post(
            f"/api/v1/gmail/accounts/{account_id}/assignments",
            json={"user_id": user.json()["id"]},
        )
        assert assignment.status_code == 403
