import base64
from unittest.mock import Mock
from uuid import UUID, uuid4

from pywebpush import WebPushException
import pytest
from sqlalchemy import select

from app.config import settings
from app.models import PushSubscription, User, UserPushPreferences
from app.services import web_push
from conftest import TestingSessionLocal


P256DH_KEY = base64.urlsafe_b64encode(b"\x04" + (b"\x01" * 64)).decode().rstrip("=")
AUTH_KEY = base64.urlsafe_b64encode(b"\x02" * 16).decode().rstrip("=")


def _session_ids(client):
    session = client.get("/api/v1/auth/me").json()
    return UUID(session["organization_id"]), UUID(session["user"]["id"])


def _configure_vapid(monkeypatch):
    monkeypatch.setattr(settings, "vapid_public_key", "test-public-key")
    monkeypatch.setattr(settings, "vapid_private_key", "test-private-key")
    monkeypatch.setattr(settings, "vapid_subject", "mailto:admin@example.test")


def test_push_subscription_and_preferences_are_user_scoped(client, monkeypatch):
    _configure_vapid(monkeypatch)
    endpoint = "https://fcm.googleapis.com/fcm/send/test-subscription"
    payload = {
        "endpoint": endpoint,
        "keys": {"p256dh": P256DH_KEY, "auth": AUTH_KEY},
    }

    created = client.post("/api/v1/notifications/push/subscriptions", json=payload)

    assert created.status_code == 201
    assert created.json()["endpoint"] == endpoint
    assert "p256dh" not in created.json()
    assert "auth" not in created.json()

    renewed = client.post("/api/v1/notifications/push/subscriptions", json=payload)
    assert renewed.status_code == 200

    preferences = client.patch(
        "/api/v1/notifications/push/preferences",
        json={
            "website_crawl_enabled": False,
            "pagespeed_enabled": True,
            "outreach_enabled": False,
        },
    )
    assert preferences.status_code == 200
    assert preferences.json() == {
        "website_crawl_enabled": False,
        "pagespeed_enabled": True,
        "outreach_enabled": False,
    }

    settings_response = client.get("/api/v1/notifications/push/settings")
    assert settings_response.status_code == 200
    assert settings_response.json()["subscriptions"][0]["endpoint"] == endpoint
    assert settings_response.json()["preferences"] == preferences.json()
    assert "p256dh" not in settings_response.text
    assert AUTH_KEY not in settings_response.text


def test_push_subscription_endpoint_cannot_be_taken_over(client, monkeypatch):
    _configure_vapid(monkeypatch)
    organization_id, user_id = _session_ids(client)
    endpoint = "https://fcm.googleapis.com/fcm/send/owned-by-another-user"

    with TestingSessionLocal() as db:
        other_user = User(
            id=uuid4(),
            organization_id=organization_id,
            email="another-user@example.test",
            password_hash="unused-test-hash",
            role="user",
        )
        db.add(other_user)
        db.add(
            PushSubscription(
                organization_id=organization_id,
                user_id=other_user.id,
                endpoint=endpoint,
                p256dh="other-public-key",
                auth="other-auth-secret",
            )
        )
        db.commit()

    response = client.post(
        "/api/v1/notifications/push/subscriptions",
        json={
            "endpoint": endpoint,
            "keys": {"p256dh": P256DH_KEY, "auth": AUTH_KEY},
        },
    )

    assert response.status_code == 409
    removed = client.request(
        "DELETE",
        "/api/v1/notifications/push/subscriptions",
        json={"endpoint": endpoint},
    )
    assert removed.status_code == 200
    assert removed.json() == {"removed": False}
    with TestingSessionLocal() as db:
        subscription = db.scalar(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        )
        assert subscription is not None
        assert subscription.user_id != user_id
        assert subscription.auth == "other-auth-secret"


def test_push_subscription_requires_configured_vapid_keys(client, monkeypatch):
    monkeypatch.setattr(settings, "vapid_public_key", None)
    monkeypatch.setattr(settings, "vapid_private_key", None)
    monkeypatch.setattr(settings, "vapid_subject", None)

    response = client.post(
        "/api/v1/notifications/push/subscriptions",
        json={
            "endpoint": "https://fcm.googleapis.com/fcm/send/test",
            "keys": {"p256dh": P256DH_KEY, "auth": AUTH_KEY},
        },
    )

    assert response.status_code == 503


def test_push_subscription_rejects_non_provider_endpoints(client, monkeypatch):
    _configure_vapid(monkeypatch)

    response = client.post(
        "/api/v1/notifications/push/subscriptions",
        json={
            "endpoint": "https://attacker.example/push",
            "keys": {"p256dh": P256DH_KEY, "auth": AUTH_KEY},
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize("status_code", [404, 410])
def test_push_sender_removes_expired_subscription(client, monkeypatch, status_code):
    _configure_vapid(monkeypatch)
    organization_id, user_id = _session_ids(client)
    endpoint = "https://fcm.googleapis.com/fcm/send/expired"

    with TestingSessionLocal() as db:
        subscription = PushSubscription(
            organization_id=organization_id,
            user_id=user_id,
            endpoint=endpoint,
            p256dh="public-key",
            auth="auth-secret",
        )
        db.add(subscription)
        db.commit()
        subscription_id = subscription.id

    monkeypatch.setattr(
        web_push,
        "webpush",
        Mock(
            side_effect=WebPushException(
                "Subscription expired",
                response=Mock(status_code=status_code),
            )
        ),
    )

    with TestingSessionLocal() as db:
        summary = web_push.send_push_to_user(
            organization_id=organization_id,
            user_id=user_id,
            category="website_crawl",
            payload={"title": "Crawl finished", "body": "The crawl is complete."},
            db=db,
        )

    assert summary.attempted == 1
    assert summary.sent == 0
    assert summary.expired_removed == 1
    with TestingSessionLocal() as db:
        assert db.get(PushSubscription, subscription_id) is None


def test_push_sender_respects_disabled_notification_category(client, monkeypatch):
    _configure_vapid(monkeypatch)
    organization_id, user_id = _session_ids(client)
    send = Mock()
    monkeypatch.setattr(web_push, "webpush", send)

    with TestingSessionLocal() as db:
        db.add(
            PushSubscription(
                organization_id=organization_id,
                user_id=user_id,
                endpoint="https://fcm.googleapis.com/fcm/send/disabled",
                p256dh="public-key",
                auth="auth-secret",
            )
        )
        db.add(
            UserPushPreferences(
                organization_id=organization_id,
                user_id=user_id,
                website_crawl_enabled=False,
                pagespeed_enabled=True,
                outreach_enabled=True,
            )
        )
        db.commit()

    with TestingSessionLocal() as db:
        summary = web_push.send_push_to_user(
            organization_id=organization_id,
            user_id=user_id,
            category="website_crawl",
            payload={"title": "Crawl finished"},
            db=db,
        )

    assert summary.attempted == 0
    assert summary.sent == 0
    send.assert_not_called()
