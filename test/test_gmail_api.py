import base64
from datetime import datetime, timezone
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
from app.api.gmail_oauth_cookie import gmail_oauth_cookie_security
from app.config import settings
from app.models import (
    ConnectedGmailAccount,
    UserGmailAssignment,
)
from app.services import gmail, gmail_oauth
from app.tenant_context import TenantContext
from conftest import TestingSessionLocal


def _invite_member(client: TestClient, email: str, password: str) -> str:
    invitation = client.post(
        "/api/v1/auth/invitations",
        json={"email": email, "role": "user"},
    )
    assert invitation.status_code == 201
    token = parse_qs(urlsplit(invitation.json()["invitation_url"]).query)["token"][0]
    with TestClient(main.app) as invitee:
        accepted = invitee.post(
            f"/api/v1/auth/invitations/{token}/accept",
            json={
                "first_name": "Test",
                "last_name": "Member",
                "password": password,
            },
        )
    assert accepted.status_code == 200
    return accepted.json()["user"]["id"]


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
        },
        headers={"referer": "http://localhost:3000/settings"},
        follow_redirects=False,
    )
    assert start.status_code == 302
    oauth_cookie = start.cookies[gmail_api.GMAIL_OAUTH_STATE_COOKIE]
    set_cookie_header = start.headers["set-cookie"].lower()
    assert "path=/api/v1/gmail/callback" in set_cookie_header
    expected_secure = settings.environment == "production"
    assert f"samesite={'none' if expected_secure else 'lax'}" in set_cookie_header
    assert ("secure" in set_cookie_header) == (
        expected_secure
    )
    google_query = parse_qs(urlsplit(start.headers["location"]).query)
    state = google_query["state"][0]
    decoded_state = gmail_oauth.decode_google_oauth_state(state)
    assert decoded_state.org_id == organization_id
    assert decoded_state.user_id == admin_user_id
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
    client.cookies.set(gmail_api.GMAIL_OAUTH_STATE_COOKIE, oauth_cookie)
    callback = client.get(
        "/api/v1/gmail/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"] == (
        "http://localhost:3000/settings?section=gmail&gmail=connected"
    )
    delete_cookie_header = callback.headers["set-cookie"].lower()
    assert "path=/api/v1/gmail/callback" in delete_cookie_header
    assert f"samesite={'none' if expected_secure else 'lax'}" in delete_cookie_header
    assert ("secure" in delete_cookie_header) == expected_secure

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
        },
        headers={"referer": "http://localhost:3000/settings"},
        follow_redirects=False,
    )
    reconnect_state = parse_qs(
        urlsplit(reconnect.headers["location"]).query
    )["state"][0]
    reconnect_cookie = reconnect.cookies[gmail_api.GMAIL_OAUTH_STATE_COOKIE]
    client.cookies.set(gmail_api.GMAIL_OAUTH_STATE_COOKIE, reconnect_cookie)
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

    user_id = _invite_member(
        client,
        "gmail-user@example.test",
        "gmail-user-password-long",
    )
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
            api_calls["thread_id"] = body.get("threadId")
            raw = body["raw"]
            raw += "=" * (-len(raw) % 4)
            sent_message = BytesParser(policy=policy.default).parsebytes(
                base64.urlsafe_b64decode(raw)
            )
            api_calls["sent_to"] = sent_message["To"]
            api_calls["sent_cc"] = sent_message.get("Cc")
            api_calls["sent_bcc"] = sent_message.get("Bcc")
            api_calls["sent_subject"] = sent_message["Subject"]
            api_calls["sent_in_reply_to"] = sent_message["In-Reply-To"]
            api_calls["sent_body"] = sent_message.get_content()
            api_calls["list_unsubscribe"] = sent_message["List-Unsubscribe"]
            api_calls["list_unsubscribe_post"] = sent_message[
                "List-Unsubscribe-Post"
            ]
            return FakeRequest(
                {"id": "sent-message-1", "threadId": "gmail-thread-1"}
            )

    class FakeLabels:
        def list(self, **parameters):
            api_calls["labels_parameters"] = parameters
            return FakeRequest(
                {
                    "labels": [
                        {"id": "INBOX", "name": "INBOX", "type": "system"},
                        {"id": "SPAM", "name": "SPAM", "type": "system"},
                        {
                            "id": "Label_123",
                            "name": "Customers",
                            "type": "user",
                        },
                    ]
                }
            )

    class FakeUsers:
        def messages(self):
            return FakeMessages()

        def labels(self):
            return FakeLabels()

    class FakeService:
        def users(self):
            return FakeUsers()

    monkeypatch.setattr(gmail, "build", lambda *_args, **_kwargs: FakeService())
    assert client.get("/api/v1/gmail/accounts/assigned").json() == []
    denied_inbox = client.get(
        f"/api/v1/gmail/accounts/{gmail_account_id}/messages"
    )
    assert denied_inbox.status_code == 403
    denied_compose = client.post(
        f"/api/v1/gmail/accounts/{gmail_account_id}/messages",
        json={
            "to": ["recipient@example.test"],
            "subject": "Hello",
            "body_text": "Message",
        },
    )
    assert denied_compose.status_code == 403

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

        labels = member_client.get(
            f"/api/v1/gmail/accounts/{gmail_account_id}/labels"
        )
        assert labels.status_code == 200
        assert labels.json()[-1]["name"] == "Customers"
        assert api_calls["labels_parameters"] == {"userId": "me"}

        inbox = member_client.get(
            f"/api/v1/gmail/accounts/{gmail_account_id}/messages",
            params={"q": "from:sender@example.com", "max_results": 5},
        )
        assert inbox.status_code == 200
        assert inbox.json()["messages"][0]["is_unread"] is True
        assert api_calls["list_parameters"]["q"] == "from:sender@example.com"
        assert api_calls["list_parameters"]["labelIds"] == ["INBOX"]

        spam = member_client.get(
            f"/api/v1/gmail/accounts/{gmail_account_id}/messages",
            params={"label_id": "SPAM"},
        )
        assert spam.status_code == 200
        assert api_calls["list_parameters"]["labelIds"] == ["SPAM"]

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
        assert api_calls["sent_body"].startswith("Thanks for reaching out.")
        assert "/unsubscribe?token=" in api_calls["sent_body"]
        assert "/unsubscribe/one-click?token=" in api_calls["list_unsubscribe"]
        assert (
            api_calls["list_unsubscribe_post"]
            == "List-Unsubscribe=One-Click"
        )

        composed = member_client.post(
            f"/api/v1/gmail/accounts/{gmail_account_id}/messages",
            json={
                "to": ["jane@example.test", "alex@example.test"],
                "cc": ["copy@example.test"],
                "bcc": ["hidden@example.test"],
                "subject": "A new message",
                "body_text": "Hello there",
            },
        )
        assert composed.status_code == 201
        assert composed.json() == {
            "id": "sent-message-1",
            "thread_id": "gmail-thread-1",
        }
        assert api_calls["sent_to"] == (
            "jane@example.test, alex@example.test"
        )
        assert api_calls["sent_cc"] == "copy@example.test"
        assert api_calls["sent_bcc"] == "hidden@example.test"
        assert api_calls["sent_subject"] == "A new message"
        assert api_calls["sent_body"].startswith("Hello there")
        assert "/unsubscribe?token=" in api_calls["sent_body"]
        assert "/unsubscribe/one-click?token=" not in api_calls["list_unsubscribe"]
        assert api_calls["list_unsubscribe_post"] is None

        invalid_compose = member_client.post(
            f"/api/v1/gmail/accounts/{gmail_account_id}/messages",
            json={
                "to": ["not an email"],
                "subject": "Invalid",
                "body_text": "Message",
            },
        )
        assert invalid_compose.status_code == 422

    accounts = client.get("/api/v1/gmail/accounts")
    assert accounts.status_code == 200
    assert [row["email_address"] for row in accounts.json()] == [
        "sender@example.com"
    ]
    assert accounts.json()[0]["assigned_users"][0]["email"] == (
        "gmail-user@example.test"
    )
    assert accounts.json()[0]["owner_user_id"] is None
    assert "encrypted_refresh_token" not in accounts.text

    removed = client.delete(
        f"/api/v1/gmail/assignments/{assignment.json()['id']}"
    )
    assert removed.status_code == 204
    refreshed_accounts = client.get("/api/v1/gmail/accounts")
    assert refreshed_accounts.json()[0]["assigned_users"] == []


def test_gmail_oauth_requires_authentication_and_assignment_requires_admin(
    client,
    monkeypatch,
):
    _configure_google_oauth(monkeypatch)
    admin_session = client.get("/api/v1/auth/me").json()
    account_id = uuid4()
    user_id = _invite_member(
        client,
        "gmail-member@example.test",
        "gmail-member-password-long",
    )

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
            },
            headers={"referer": "http://localhost:3000/settings"},
            follow_redirects=False,
        )
        assert oauth_start.status_code == 302
        assignment = member_client.post(
            f"/api/v1/gmail/accounts/{account_id}/assignments",
            json={"user_id": user_id},
        )
        assert assignment.status_code == 403


def test_member_cannot_reconnect_an_existing_shared_gmail_account(
    client,
    monkeypatch,
):
    _configure_google_oauth(monkeypatch)
    session = client.get("/api/v1/auth/me").json()
    organization_id = UUID(session["organization_id"])
    with TestingSessionLocal() as db:
        account = ConnectedGmailAccount(
            org_id=organization_id,
            email_address="shared@example.com",
            google_account_id="existing-shared-google-account",
            encrypted_refresh_token=gmail.encrypt_refresh_token(
                "existing-shared-refresh-token"
            ),
            token_updated_at=datetime.now(timezone.utc),
        )
        db.add(account)
        db.commit()
        account_id = account.id

    _invite_member(
        client,
        "shared-attempt@example.test",
        "shared-attempt-password-long",
    )

    async def exchange_code(code: str):
        return gmail_oauth.GoogleOAuthTokens(
            access_token="shared-attempt-access",
            refresh_token="replacement-shared-refresh-token",
        )

    async def fetch_profile(access_token: str):
        return gmail_oauth.GoogleProfile(
            google_account_id="existing-shared-google-account",
            email_address="shared@example.com",
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

    with TestClient(main.app) as member_client:
        login = member_client.post(
            "/api/v1/auth/login",
            json={
                "email": "shared-attempt@example.test",
                "password": "shared-attempt-password-long",
            },
        )
        assert login.status_code == 200
        start = member_client.get(
            "/api/v1/auth/google/login",
            params={"org_id": str(organization_id)},
            headers={"referer": "http://localhost:3000/gmail"},
            follow_redirects=False,
        )
        assert start.status_code == 302
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        member_client.cookies.set(
            gmail_api.GMAIL_OAUTH_STATE_COOKIE,
            start.cookies[gmail_api.GMAIL_OAUTH_STATE_COOKIE],
        )
        callback = member_client.get(
            "/api/v1/gmail/callback",
            params={"code": "shared-attempt", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == (
            "http://localhost:3000/gmail?gmail=already-connected"
        )

    with TestingSessionLocal() as db:
        account = db.get(ConnectedGmailAccount, account_id)
        assert account is not None
        assert gmail.decrypt_refresh_token(account.encrypted_refresh_token) == (
            "existing-shared-refresh-token"
        )


def test_member_connects_personal_gmail_assigned_only_to_self(
    client,
    monkeypatch,
):
    _configure_google_oauth(monkeypatch)
    session = client.get("/api/v1/auth/me").json()
    organization_id = UUID(session["organization_id"])
    owner_id = _invite_member(
        client,
        "personal-owner@example.test",
        "personal-owner-password-long",
    )
    other_member_id = _invite_member(
        client,
        "personal-other@example.test",
        "personal-other-password-long",
    )

    async def exchange_code(code: str):
        return gmail_oauth.GoogleOAuthTokens(
            access_token=f"access-{code}",
            refresh_token=f"refresh-{code}",
        )

    async def fetch_profile(access_token: str):
        return gmail_oauth.GoogleProfile(
            google_account_id="member-owned-google-account",
            email_address="personal@example.com",
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

    with TestClient(main.app) as owner_client:
        login = owner_client.post(
            "/api/v1/auth/login",
            json={
                "email": "personal-owner@example.test",
                "password": "personal-owner-password-long",
            },
        )
        assert login.status_code == 200
        start = owner_client.get(
            "/api/v1/auth/google/login",
            params={"org_id": str(organization_id)},
            headers={"referer": "http://localhost:3000/gmail"},
            follow_redirects=False,
        )
        assert start.status_code == 302
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        oauth_cookie = start.cookies[gmail_api.GMAIL_OAUTH_STATE_COOKIE]
        assert gmail_oauth.decode_google_oauth_state(state).user_id == UUID(
            owner_id
        )
        owner_client.cookies.set(
            gmail_api.GMAIL_OAUTH_STATE_COOKIE,
            oauth_cookie,
        )
        callback = owner_client.get(
            "/api/v1/gmail/callback",
            params={"code": "owner-connect", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303, callback.text
        assert callback.headers["location"] == (
            "http://localhost:3000/gmail?gmail=connected"
        )
        assigned_accounts = owner_client.get(
            "/api/v1/gmail/accounts/assigned"
        )
        assert [
            account["email_address"] for account in assigned_accounts.json()
        ] == ["personal@example.com"]
        reauth_start = owner_client.get(
            "/api/v1/auth/google/login",
            params={"org_id": str(organization_id)},
            headers={"referer": "http://localhost:3000/gmail"},
            follow_redirects=False,
        )
        reauth_state = parse_qs(
            urlsplit(reauth_start.headers["location"]).query
        )["state"][0]
        owner_client.cookies.set(
            gmail_api.GMAIL_OAUTH_STATE_COOKIE,
            reauth_start.cookies[gmail_api.GMAIL_OAUTH_STATE_COOKIE],
        )
        reauthorized = owner_client.get(
            "/api/v1/gmail/callback",
            params={"code": "owner-reauthorize", "state": reauth_state},
            follow_redirects=False,
        )
        assert reauthorized.status_code == 303
        assert reauthorized.headers["location"] == (
            "http://localhost:3000/gmail?gmail=connected"
        )

    with TestingSessionLocal() as db:
        account = db.scalar(
            select(ConnectedGmailAccount).where(
                ConnectedGmailAccount.google_account_id
                == "member-owned-google-account"
            )
        )
        assert account is not None
        assert account.owner_user_id == UUID(owner_id)
        original_refresh_token = gmail.decrypt_refresh_token(
            account.encrypted_refresh_token
        )
        assignment = db.scalar(
            select(UserGmailAssignment).where(
                UserGmailAssignment.gmail_account_id == account.id
            )
        )
        assert assignment is not None
        assert assignment.user_id == UUID(owner_id)
        account_id = account.id

    assert client.post(
        f"/api/v1/gmail/accounts/{account_id}/assignments",
        json={"user_id": other_member_id},
    ).status_code == 403
    with TestingSessionLocal() as db:
        assignment = db.scalar(
            select(UserGmailAssignment).where(
                UserGmailAssignment.gmail_account_id == account_id
            )
        )
        assert assignment is not None
        assignment_id = assignment.id
    assert client.delete(
        f"/api/v1/gmail/assignments/{assignment_id}"
    ).status_code == 403

    with TestClient(main.app) as other_client:
        login = other_client.post(
            "/api/v1/auth/login",
            json={
                "email": "personal-other@example.test",
                "password": "personal-other-password-long",
            },
        )
        assert login.status_code == 200
        start = other_client.get(
            "/api/v1/auth/google/login",
            params={"org_id": str(organization_id)},
            headers={"referer": "http://localhost:3000/gmail"},
            follow_redirects=False,
        )
        assert start.status_code == 302
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        other_client.cookies.set(
            gmail_api.GMAIL_OAUTH_STATE_COOKIE,
            start.cookies[gmail_api.GMAIL_OAUTH_STATE_COOKIE],
        )
        callback = other_client.get(
            "/api/v1/gmail/callback",
            params={"code": "other-connect", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == (
            "http://localhost:3000/gmail?gmail=already-connected"
        )

    with TestingSessionLocal() as db:
        account = db.get(ConnectedGmailAccount, account_id)
        assert account is not None
        assert gmail.decrypt_refresh_token(account.encrypted_refresh_token) == (
            original_refresh_token
        )


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ("development", (False, "lax")),
        ("production", (True, "none")),
    ],
)
def test_gmail_oauth_cookie_security_matches_environment(
    monkeypatch,
    environment,
    expected,
):
    monkeypatch.setattr(settings, "environment", environment)

    assert gmail_oauth_cookie_security() == expected
