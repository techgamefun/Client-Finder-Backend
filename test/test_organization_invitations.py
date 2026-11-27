from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.main import app


def _create_invitation(
    admin: TestClient,
    email: str,
    *,
    role: str = "user",
) -> tuple[dict, str]:
    response = admin.post(
        "/api/v1/auth/invitations",
        json={"email": email, "role": role},
    )
    assert response.status_code == 201
    invitation_url = urlparse(response.json()["invitation_url"])
    return response.json(), parse_qs(invitation_url.query)["token"][0]


def test_new_invitee_can_create_account_and_join_inviting_organization(client):
    invitation, token = _create_invitation(
        client,
        "new-member@example.test",
        role="admin",
    )
    with TestClient(app) as invitee:
        accepted = invitee.post(
            f"/api/v1/auth/invitations/{token}/accept",
            json={
                "first_name": "New",
                "last_name": "Member",
                "password": "new-member-password",
            },
        )

    assert accepted.status_code == 200
    session = accepted.json()
    assert session["user"]["email"] == invitation["email"]
    assert session["user"]["role"] == "admin"
    assert session["organization_id"] == client.get(
        "/api/v1/auth/me"
    ).json()["organization_id"]
    assert len(session["organizations"]) == 1
    assert [
        user["email"]
        for user in client.get("/api/v1/auth/users").json()["users"]
    ] == ["admin@example.test", "new-member@example.test"]
    assert client.get("/api/v1/auth/invitations").json() == []


def test_existing_account_joins_second_organization_and_can_switch(client):
    with TestClient(app) as existing_user:
        created = existing_user.post(
            "/api/v1/auth/register-organization",
            json={
                "organization_name": "Existing User Organization",
                "email": "existing-member@example.test",
                "password": "existing-user-password",
                "first_name": "Existing",
                "last_name": "Member",
            },
        )
        assert created.status_code == 200
        original_org_id = created.json()["organization_id"]

        invitation, token = _create_invitation(
            client,
            "existing-member@example.test",
            role="user",
        )
        existing_user.cookies.clear()
        accepted = existing_user.post(
            f"/api/v1/auth/invitations/{token}/accept",
            json={"password": "existing-user-password"},
        )

        assert accepted.status_code == 200
        joined_session = accepted.json()
        invited_org_id = client.get("/api/v1/auth/me").json()["organization_id"]
        assert joined_session["organization_id"] == invited_org_id
        assert len(joined_session["organizations"]) == 2
        assert joined_session["user"]["role"] == invitation["role"]

        switched = existing_user.post(
            "/api/v1/auth/select-organization",
            json={"organization_id": original_org_id},
        )
        assert switched.status_code == 200
        assert switched.json()["organization_id"] == original_org_id
        assert switched.json()["user"]["role"] == "admin"
        assert existing_user.get("/api/v1/websites").json() == []

        switch_back = existing_user.post(
            "/api/v1/auth/select-organization",
            json={"organization_id": invited_org_id},
        )
        assert switch_back.status_code == 200
        assert existing_user.get("/api/v1/websites").json() == []


def test_invitation_link_is_single_use_and_revocable(client):
    _, token = _create_invitation(client, "revoke@example.test")
    pending = client.get("/api/v1/auth/invitations")
    assert pending.status_code == 200
    invitation_id = pending.json()[0]["id"]

    revoked = client.delete(f"/api/v1/auth/invitations/{invitation_id}")
    assert revoked.status_code == 204
    preview = client.get(f"/api/v1/auth/invitations/{token}")
    assert preview.status_code == 404

    _, accepted_token = _create_invitation(client, "single-use@example.test")
    with TestClient(app) as invitee:
        first_acceptance = invitee.post(
            f"/api/v1/auth/invitations/{accepted_token}/accept",
            json={
                "first_name": "Single",
                "last_name": "Use",
                "password": "single-use-password",
            },
        )
        replay = invitee.post(
            f"/api/v1/auth/invitations/{accepted_token}/accept",
            json={"password": "single-use-password"},
        )

    assert first_acceptance.status_code == 200
    assert replay.status_code == 404
