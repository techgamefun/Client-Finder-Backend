from uuid import UUID
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.auth import (
    ACCESS_TOKEN_COOKIE,
    REFRESH_TOKEN_COOKIE,
    access_token_duration_seconds,
    refresh_token_duration_seconds,
)
from app.models import Website
from app.tenant_context import TenantContext
from conftest import TestingSessionLocal


def _invite_user(
    admin_client: TestClient,
    *,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
    role: str = "user",
):
    invitation = admin_client.post(
        "/api/v1/auth/invitations",
        json={"email": email, "role": role},
    )
    assert invitation.status_code == 201
    invitation_url = urlparse(invitation.json()["invitation_url"])
    token = parse_qs(invitation_url.query)["token"][0]
    with TestClient(app) as invitee:
        accepted = invitee.post(
            f"/api/v1/auth/invitations/{token}/accept",
            json={
                "first_name": first_name,
                "last_name": last_name,
                "password": password,
            },
        )
    assert accepted.status_code == 200
    return accepted


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
    assert client.get("/api/v1/websites").status_code == 401

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
    assert client.get("/api/v1/websites").status_code == 200

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


def test_signup_starts_in_private_personal_workspace_then_can_create_org(client):
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": "personal-user@example.test",
            "password": "personal-password-long",
            "first_name": "Taylor",
            "last_name": "Reed",
        },
    )
    assert registration.status_code == 200
    personal_session = registration.json()
    personal_workspace = personal_session["organizations"][0]
    assert personal_workspace["is_personal"] is True
    assert personal_workspace["name"] == "Taylor's Personal Workspace"
    assert personal_session["organization_id"] == personal_workspace["id"]

    with TestingSessionLocal() as db:
        context = TenantContext(
            organization_id=UUID(personal_workspace["id"]),
            user_id=UUID(personal_session["user"]["id"]),
            email=personal_session["user"]["email"],
            role=personal_session["user"]["role"],
        )
        db.info["tenant_context"] = context
        personal_website = Website(
            organization_id=context.organization_id,
            created_by_user_id=context.user_id,
            website_url="https://personal.example/",
            domain="personal.example",
        )
        db.add(personal_website)
        db.commit()
        personal_website_id = personal_website.id

    organization = client.post(
        "/api/v1/auth/organizations",
        json={"name": "Taylor's Team"},
    )
    assert organization.status_code == 201
    organization_session = organization.json()
    team_workspace = next(
        workspace
        for workspace in organization_session["organizations"]
        if workspace["name"] == "Taylor's Team"
    )
    assert team_workspace["is_personal"] is False
    assert organization_session["organization_id"] == team_workspace["id"]
    assert client.get(f"/api/v1/websites/{personal_website_id}").status_code == 404

    selected_personal = client.post(
        "/api/v1/auth/select-organization",
        json={"organization_id": personal_workspace["id"]},
    )
    assert selected_personal.status_code == 200
    assert selected_personal.json()["organization_id"] == personal_workspace["id"]
    assert [
        website["domain"] for website in client.get("/api/v1/websites").json()
    ] == ["personal.example"]
    assert client.post(
        "/api/v1/auth/invitations",
        json={"email": "invite@example.test"},
    ).status_code == 409


def test_legacy_session_cookie_is_not_accepted(client):
    client.cookies.clear()
    client.cookies.set(
        "lead_intelligence_session",
        "previous-release-session",
        domain="testserver.local",
        path="/",
    )

    assert client.get("/api/v1/websites").status_code == 401
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
    first_member = _invite_user(
        client,
        email="first-member@example.test",
        password="member-password-long",
        first_name="First",
        last_name="Member",
    )

    with TestingSessionLocal() as db:
        first_context = db.info["tenant_context"]
        first_website = Website(
            organization_id=first_context.organization_id,
            created_by_user_id=first_context.user_id,
            website_url="https://first.example/",
            domain="shared.example",
        )
        db.add(first_website)
        db.commit()
        first_website_id = first_website.id
    first_contact = client.post(
        f"/api/v1/websites/{first_website_id}/contacts",
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
            f"/api/v1/auth/users/{first_member.json()['user']['id']}/role",
            json={"role": "admin"},
        )
        assert cross_tenant_role_change.status_code == 404

        second_session = registration.json()
        with TestingSessionLocal() as db:
            db.info["tenant_context"] = TenantContext(
                organization_id=UUID(second_session["organization_id"]),
                user_id=UUID(second_session["user"]["id"]),
                email=second_session["user"]["email"],
                role=second_session["user"]["role"],
            )
            second_website = Website(
                organization_id=UUID(second_session["organization_id"]),
                created_by_user_id=UUID(second_session["user"]["id"]),
                website_url="https://second.example/",
                domain="shared.example",
            )
            db.add(second_website)
            db.commit()
            second_website_id = second_website.id

        assert second_client.get(
            f"/api/v1/websites/{first_website_id}"
        ).status_code == 404
        assert second_client.get(
            f"/api/v1/websites/{first_website_id}/contacts"
        ).status_code == 404
        assert second_client.patch(
            f"/api/v1/websites/{second_website_id}/contacts/"
            f"{first_contact.json()['id']}",
            json={"name": "Cross-tenant edit"},
        ).status_code == 404

    first_list = client.get("/api/v1/websites")
    assert first_list.status_code == 200
    assert [row["domain"] for row in first_list.json()] == ["shared.example"]
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

    created = _invite_user(
        client,
        first_name="Taylor",
        last_name="Reed",
        email="taylor.reed@example.test",
        password="member-password-long",
    )
    assert created.json()["user"]["first_name"] == "Taylor"
    assert created.json()["user"]["last_name"] == "Reed"
    user_id = created.json()["user"]["id"]

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
    created = _invite_user(
        client,
        first_name="Morgan",
        last_name="Lane",
        email="member@example.test",
        password="member-password-long",
    )
    assert created.json()["user"]["first_name"] == "Morgan"
    assert created.json()["user"]["last_name"] == "Lane"

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
                f"/api/v1/auth/users/{created.json()['user']['id']}/role",
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
