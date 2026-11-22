import asyncio

from fastapi.testclient import TestClient

from app import main
from app.api import media as media_api
from conftest import create_invited_member


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 16)


def test_authenticated_user_can_upload_and_remove_own_profile_photo(
    client,
    monkeypatch,
):
    uploaded: list[str] = []
    deleted: list[str] = []

    async def upload_image(content, content_type, public_id):
        assert content.startswith(b"\x89PNG")
        assert content_type == "image/png"
        uploaded.append(public_id)
        return "https://res.cloudinary.com/test-cloud/image/upload/profile.png"

    async def delete_image(public_id):
        deleted.append(public_id)

    monkeypatch.setattr(media_api, "_upload_image", upload_image)
    monkeypatch.setattr(media_api, "_delete_image", delete_image)

    response = client.post(
        "/api/v1/media/profile-photo",
        files={"file": ("profile.png", PNG_BYTES, "image/png")},
    )

    assert response.status_code == 200
    assert response.json()["url"].endswith("/profile.png")
    assert uploaded[0].startswith("users/")

    session = client.get("/api/v1/auth/me")
    assert session.status_code == 200
    assert session.json()["user"]["profile_photo_url"] == response.json()["url"]

    removed = client.delete("/api/v1/media/profile-photo")
    assert removed.status_code == 204
    assert deleted == uploaded

    updated_session = client.get("/api/v1/auth/me")
    assert updated_session.json()["user"]["profile_photo_url"] is None


def test_admin_can_upload_and_remove_organization_branding(client, monkeypatch):
    async def upload_image(_content, _content_type, public_id):
        return (
            "https://res.cloudinary.com/test-cloud/image/upload/"
            f"{public_id.rsplit('/', maxsplit=1)[-1]}.png"
        )

    deleted: list[str] = []

    async def delete_image(public_id):
        deleted.append(public_id)

    monkeypatch.setattr(media_api, "_upload_image", upload_image)
    monkeypatch.setattr(media_api, "_delete_image", delete_image)

    logo = client.post(
        "/api/v1/media/organization/logo",
        files={"file": ("logo.png", PNG_BYTES, "image/png")},
    )
    banner = client.post(
        "/api/v1/media/organization/banner",
        files={"file": ("banner.png", PNG_BYTES, "image/png")},
    )

    assert logo.status_code == 200
    assert banner.status_code == 200
    session = client.get("/api/v1/auth/me")
    assert session.json()["organization_logo_url"] == logo.json()["url"]
    assert session.json()["organization_banner_url"] == banner.json()["url"]

    removed = client.delete("/api/v1/media/organization/logo")
    assert removed.status_code == 204
    assert deleted[0].endswith("/logo")

    updated_session = client.get("/api/v1/auth/me")
    assert updated_session.json()["organization_logo_url"] is None
    assert updated_session.json()["organization_banner_url"] == banner.json()["url"]


def test_organization_image_upload_requires_an_admin(client, monkeypatch):
    create_invited_member(
        client,
        email="media-member@example.test",
        password="media-member-password",
    )

    upload_calls: list[str] = []

    async def upload_image(_content, _content_type, public_id):
        upload_calls.append(public_id)
        return "https://res.cloudinary.com/test-cloud/image/upload/logo.png"

    monkeypatch.setattr(media_api, "_upload_image", upload_image)

    with TestClient(main.app) as member_client:
        login = member_client.post(
            "/api/v1/auth/login",
            json={
                "email": "media-member@example.test",
                "password": "media-member-password",
            },
        )
        assert login.status_code == 200
        response = member_client.post(
            "/api/v1/media/organization/logo",
            files={"file": ("logo.png", PNG_BYTES, "image/png")},
        )

    assert response.status_code == 403
    assert upload_calls == []


def test_media_upload_rejects_mismatched_image_content(client):
    response = client.post(
        "/api/v1/media/profile-photo",
        files={"file": ("not-image.png", b"not an image", "image/png")},
    )

    assert response.status_code == 415


def test_media_upload_reports_missing_cloudinary_configuration(
    client,
    monkeypatch,
):
    monkeypatch.setattr(media_api.settings, "cloudinary_cloud_name", None)
    monkeypatch.setattr(media_api.settings, "cloudinary_api_key", None)
    monkeypatch.setattr(media_api.settings, "cloudinary_api_secret", None)

    response = client.post(
        "/api/v1/media/profile-photo",
        files={"file": ("profile.png", PNG_BYTES, "image/png")},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Cloudinary image storage is not configured."
    )


def test_cloudinary_upload_uses_server_side_basic_auth(monkeypatch):
    sent_requests: list[dict[str, object]] = []

    class FakeResponse:
        is_success = True
        status_code = 200

        @staticmethod
        def json():
            return {
                "secure_url": (
                    "https://res.cloudinary.com/test-cloud/image/upload/"
                    "users/test/profile-photo.png"
                )
            }

    class FakeClient:
        def __init__(self, timeout):
            assert timeout == 45

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, data, files, auth):
            sent_requests.append(
                {"url": url, "data": data, "files": files, "auth": auth}
            )
            return FakeResponse()

    monkeypatch.setattr(media_api.settings, "cloudinary_cloud_name", "test-cloud")
    monkeypatch.setattr(media_api.settings, "cloudinary_api_key", "test-key")
    monkeypatch.setattr(media_api.settings, "cloudinary_api_secret", "test-secret")
    monkeypatch.setattr(media_api.httpx, "AsyncClient", FakeClient)

    image_url = asyncio.run(
        media_api._upload_image(
            PNG_BYTES,
            "image/png",
            "users/test/profile-photo",
        )
    )

    request = sent_requests[0]

    assert request["url"] == (
        "https://api.cloudinary.com/v1_1/test-cloud/image/upload"
    )
    assert request["auth"] == ("test-key", "test-secret")
    assert request["data"] == {
        "overwrite": "true",
        "public_id": "users/test/profile-photo",
    }
    assert image_url.startswith("https://res.cloudinary.com/test-cloud/")
