from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.auth import (
    ACCESS_TOKEN_COOKIE,
    REFRESH_TOKEN_COOKIE,
    access_token_duration_seconds,
    refresh_token_duration_seconds,
)


def test_refresh_rotates_tokens_and_revokes_the_previous_refresh_token(client):
    old_access_token = client.cookies.get(ACCESS_TOKEN_COOKIE)
    old_refresh_token = client.cookies.get(REFRESH_TOKEN_COOKIE)
    assert old_access_token
    assert old_refresh_token

    client.cookies.set(
        ACCESS_TOKEN_COOKIE,
        "invalid-access-token",
        domain="testserver.local",
        path="/",
    )
    assert client.get("/api/v1/companies").status_code == 401

    refreshed = client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    refreshed_cookies = refreshed.headers.get_list("set-cookie")
    assert any(
        "lead_intelligence_access=" in cookie
        and f"Max-Age={access_token_duration_seconds()}" in cookie
        and "HttpOnly" in cookie
        for cookie in refreshed_cookies
    )
    assert any(
        "lead_intelligence_refresh=" in cookie
        and f"Max-Age={refresh_token_duration_seconds()}" in cookie
        and "Path=/api/v1/auth" in cookie
        and "HttpOnly" in cookie
        for cookie in refreshed_cookies
    )
    assert "access_token" not in refreshed.json()
    assert client.cookies.get(ACCESS_TOKEN_COOKIE) != "invalid-access-token"
    assert client.cookies.get(REFRESH_TOKEN_COOKIE) != old_refresh_token
    assert client.get("/api/v1/companies").status_code == 200

    replay_client = TestClient(app)
    replay = replay_client.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": f"{REFRESH_TOKEN_COOKIE}={old_refresh_token}"},
    )
    assert replay.status_code == 401


def test_configured_separate_token_secrets_and_expiries(client, monkeypatch):
    monkeypatch.setattr(settings, "access_token_secret", "11" * 32)
    monkeypatch.setattr(settings, "refresh_token_secret", "22" * 32)
    monkeypatch.setattr(settings, "access_token_expiry", "2m")
    monkeypatch.setattr(settings, "refresh_token_expiry", "8h")

    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": "admin@example.test",
            "password": "test-password-long-enough",
        },
    )

    assert response.status_code == 200
    cookie_headers = response.headers.get_list("set-cookie")
    assert any(
        "lead_intelligence_access=" in cookie and "Max-Age=120" in cookie
        for cookie in cookie_headers
    )
    assert any(
        "lead_intelligence_refresh=" in cookie and "Max-Age=28800" in cookie
        for cookie in cookie_headers
    )
    assert client.get("/api/v1/auth/me").status_code == 200

    client.cookies.set(
        ACCESS_TOKEN_COOKIE,
        "expired-access-token",
        domain="testserver.local",
        path="/",
    )
    refreshed = client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200


def test_unauthenticated_business_api_is_rejected(client):
    client.cookies.clear()

    response = client.get("/api/v1/companies")

    assert response.status_code == 401


def test_account_profile_updates_names(client):
    response = client.patch(
        "/api/v1/auth/me/profile",
        json={"first_name": "  Updated  ", "last_name": "  Name  "},
    )

    assert response.status_code == 200
    assert response.json()["user"]["first_name"] == "Updated"
    assert response.json()["user"]["last_name"] == "Name"
    assert client.get("/api/v1/auth/me").json()["user"]["first_name"] == "Updated"


def test_legacy_session_cookie_is_not_accepted(client):
    client.cookies.clear()
    client.cookies.set(
        "lead_intelligence_session",
        "previous-release-session",
        domain="testserver.local",
        path="/",
    )

    assert client.get("/api/v1/companies").status_code == 401
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_password_change_revokes_other_sessions(client):
    with TestClient(app) as other_session:
        login = other_session.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@example.test",
                "password": "test-password-long-enough",
            },
        )
        assert login.status_code == 200

        changed = client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "test-password-long-enough",
                "new_password": "new-password-long-enough",
            },
        )
        assert changed.status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 200
        assert other_session.get("/api/v1/auth/me").status_code == 401
        assert other_session.post("/api/v1/auth/refresh").status_code == 401

        old_password_login = other_session.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@example.test",
                "password": "test-password-long-enough",
            },
        )
        assert old_password_login.status_code == 401


def test_organizations_cannot_read_or_overwrite_each_others_data(client):
    first_member = client.post(
        "/api/v1/auth/users",
        json={
            "email": "first-member@example.test",
            "password": "member-password-long",
            "role": "user",
        },
    )
    assert first_member.status_code == 201

    first_company = client.post(
        "/api/v1/companies",
        json={"name": "First tenant", "domain": "shared.example"},
    )
    assert first_company.status_code == 201
    first_contact = client.post(
        f"/api/v1/companies/{first_company.json()['id']}/contacts",
        json={"name": "Private first-tenant contact"},
    )
    assert first_contact.status_code == 201

    with TestClient(app) as second_client:
        registration = second_client.post(
            "/api/v1/auth/register-organization",
            json={
                "organization_name": "Second Organization",
                "organization_description": "Second tenant profile",
                "email": "second-admin@example.test",
                "password": "another-long-password",
                "first_name": "Second",
                "last_name": "Admin",
            },
        )
        assert registration.status_code == 200
        assert (
            registration.json()["organization_description"]
            == "Second tenant profile"
        )
        cross_tenant_role_change = second_client.patch(
            f"/api/v1/auth/users/{first_member.json()['id']}/role",
            json={"role": "admin"},
        )
        assert cross_tenant_role_change.status_code == 404

        second_company = second_client.post(
            "/api/v1/companies",
            json={"name": "Second tenant", "domain": "shared.example"},
        )
        assert second_company.status_code == 201

        assert second_client.get(
            f"/api/v1/companies/{first_company.json()['id']}"
        ).status_code == 404
        assert second_client.get(
            f"/api/v1/companies/{first_company.json()['id']}/contacts"
        ).status_code == 404
        assert second_client.get(
            f"/api/v1/companies/{second_company.json()['id']}/contacts/"
            f"{first_contact.json()['id']}"
        ).status_code == 404

    first_list = client.get("/api/v1/companies")
    assert first_list.status_code == 200
    assert [row["name"] for row in first_list.json()] == ["First tenant"]
    assert first_list.json()[0]["created_by_email"] == "admin@example.test"


def test_organization_description_member_names_and_admin_role_changes(client):
    session = client.get("/api/v1/auth/me")
    assert session.status_code == 200
    assert session.json()["organization_description"] is None

    updated_profile = client.patch(
        "/api/v1/auth/organization",
        json={"description": "  A practical description.  "},
    )
    assert updated_profile.status_code == 200
    assert updated_profile.json()["description"] == "A practical description."
    assert (
        client.get("/api/v1/auth/me").json()["organization_description"]
        == "A practical description."
    )

    created = client.post(
        "/api/v1/auth/users",
        json={
            "first_name": "Taylor",
            "last_name": "Reed",
            "email": "taylor.reed@example.test",
            "password": "member-password-long",
            "role": "user",
        },
    )
    assert created.status_code == 201
    assert created.json()["first_name"] == "Taylor"
    assert created.json()["last_name"] == "Reed"
    user_id = created.json()["id"]

    promoted = client.patch(
        f"/api/v1/auth/users/{user_id}/role",
        json={"role": "admin"},
    )
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"

    demoted = client.patch(
        f"/api/v1/auth/users/{user_id}/role",
        json={"role": "user"},
    )
    assert demoted.status_code == 200
    assert demoted.json()["role"] == "user"

    current_user_id = session.json()["user"]["id"]
    self_role_change = client.patch(
        f"/api/v1/auth/users/{current_user_id}/role",
        json={"role": "user"},
    )
    assert self_role_change.status_code == 409


def test_user_roles_and_provider_keys_are_scoped(client):
    created = client.post(
        "/api/v1/auth/users",
        json={
            "first_name": "Morgan",
            "last_name": "Lane",
            "email": "member@example.test",
            "password": "member-password-long",
            "role": "user",
        },
    )
    assert created.status_code == 201
    assert created.json()["first_name"] == "Morgan"
    assert created.json()["last_name"] == "Lane"

    with TestClient(app) as member_client:
        login = member_client.post(
            "/api/v1/auth/login",
            json={
                "email": "member@example.test",
                "password": "member-password-long",
            },
        )
        assert login.status_code == 200
        assert member_client.get("/api/v1/auth/users").status_code == 403
        assert (
            member_client.patch(
                f"/api/v1/auth/users/{created.json()['id']}/role",
                json={"role": "admin"},
            ).status_code
            == 403
        )
        assert member_client.put(
            "/api/v1/settings",
            json={"crawl_max_pages": 2},
        ).status_code == 403

        saved = member_client.put(
            "/api/v1/settings",
            json={"tavily_api_key": "members-private-provider-key"},
        )
        assert saved.status_code == 200
        assert saved.json()["tavily_configured"] is True
        assert "members-private-provider-key" not in saved.text

    admin_settings = client.get("/api/v1/settings")
    assert admin_settings.status_code == 200
    assert admin_settings.json()["tavily_configured"] is False
