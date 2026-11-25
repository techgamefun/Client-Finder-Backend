import base64
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from uuid import UUID, uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.models import (
    Company,
    ConnectedGmailAccount,
    Contact,
    UserGmailAssignment,
    Website,
    WebsiteCrawl,
)
from app.services import gmail
from app.api import email_templates as email_templates_api
from app.api import settings as settings_api
from conftest import TestingSessionLocal, create_invited_member


def _session_ids(client):
    session = client.get("/api/v1/auth/me").json()
    return UUID(session["organization_id"]), UUID(session["user"]["id"])


def _create_company_with_contact(organization_id, *, email="jane@example.test"):
    with TestingSessionLocal() as db:
        company = Company(
            organization_id=organization_id,
            name="Example Manufacturing",
            domain="example.test",
            website="https://example.test",
            industry="Manufacturing",
            city="Austin",
            lead_status="qualified",
            lead_score=82,
            software_need="Inventory planning",
        )
        crawl = WebsiteCrawl(
            organization_id=organization_id,
            website_url="https://example.test",
            home_content="We build useful products.",
            about_content="A locally owned manufacturer.",
        )
        db.add_all([company, crawl])
        db.flush()
        website = Website(
            organization_id=organization_id,
            website_url="https://example.test",
            domain="example.test",
            crawl_id=crawl.id,
        )
        contact = Contact(
            organization_id=organization_id,
            company_id=company.id,
            name=None,
            role="Operations Director",
            business_email=email,
            business_phone="+1 555 0100",
        )
        db.add_all([website, contact])
        db.commit()
        return company.id, contact.id


def test_email_template_crud_and_organization_scoping(client):
    organization_id, _ = _session_ids(client)
    endpoint = f"/api/v1/orgs/{organization_id}/templates"

    variables = client.get(f"{endpoint}/variables")
    assert variables.status_code == 200
    placeholders = {item["placeholder"] for item in variables.json()}
    assert "{{ company.phone }}" in placeholders
    assert "{{ company.crawled_text }}" in placeholders
    assert "{{ contact.first_name }}" in placeholders
    assert "{{ sender.email }}" in placeholders

    created = client.post(
        endpoint,
        json={
            "name": "First touch",
            "subject": "Hello {{ contact.first_name | default('there') }}",
            "body_content": "<p>Hello {{ contact.first_name | default('there') }}</p>",
        },
    )
    assert created.status_code == 201
    template = created.json()
    assert template["org_id"] == str(organization_id)

    listed = client.get(endpoint)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [template["id"]]

    fetched = client.get(f"{endpoint}/{template['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "First touch"

    updated = client.patch(
        f"{endpoint}/{template['id']}",
        json={"name": "Updated first touch"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Updated first touch"
    assert client.patch(
        f"{endpoint}/{template['id']}",
        json={"name": None},
    ).status_code == 422

    invalid_template = client.post(
        endpoint,
        json={
            "name": "Invalid",
            "subject": "Hello {{",
            "body_content": "<p>Body</p>",
        },
    )
    assert invalid_template.status_code == 422

    assert client.get(
        f"/api/v1/orgs/{uuid4()}/templates"
    ).status_code == 404

    create_invited_member(
        client,
        email="template-member@example.test",
        password="template-member-password-long",
    )
    with TestClient(main.app) as member_client:
        login = member_client.post(
            "/api/v1/auth/login",
            json={
                "email": "template-member@example.test",
                "password": "template-member-password-long",
            },
        )
        assert login.status_code == 200
        assert member_client.get(endpoint).status_code == 200
        assert member_client.get(f"{endpoint}/variables").status_code == 200
        assert member_client.get(f"{endpoint}/{template['id']}").status_code == 200
        assert member_client.post(
            endpoint,
            json={
                "name": "Member template",
                "subject": "Hello",
                "body_content": "Body",
            },
        ).status_code == 403
        assert member_client.post(
            f"{endpoint}/generate-draft",
            json={"prompt": "A concise introduction for a new prospect"},
        ).status_code == 403

    assert client.delete(f"{endpoint}/{template['id']}").status_code == 204
    assert client.get(f"{endpoint}/{template['id']}").status_code == 404


def test_generate_email_template_draft_uses_personal_key_without_saving(
    client,
    monkeypatch,
):
    organization_id, user_id = _session_ids(client)
    endpoint = f"/api/v1/orgs/{organization_id}/templates"
    api_key = "test-gemini-api-key"
    saved_settings = client.put(
        "/api/v1/settings",
        json={"gemini_api_key": api_key},
    )
    assert saved_settings.status_code == 200

    async def fake_generate(prompt, received_key):
        assert prompt == "Write a friendly introductory message"
        assert received_key == api_key
        return email_templates_api.EmailTemplateDraftResponse(
            name="Friendly introduction",
            subject="A quick introduction",
            body_content="<p>Hello {{ contact.first_name }}</p>",
        )

    monkeypatch.setattr(
        email_templates_api,
        "generate_email_template_draft",
        fake_generate,
    )
    response = client.post(
        f"{endpoint}/generate-draft",
        json={"prompt": "Write a friendly introductory message"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "name": "Friendly introduction",
        "subject": "A quick introduction",
        "body_content": "<p>Hello {{ contact.first_name }}</p>",
    }
    assert client.get(endpoint).json() == []
    with TestingSessionLocal() as db:
        context = db.info["tenant_context"]
        assert context.user_id == user_id


def test_generate_email_template_draft_requires_personal_key(client):
    organization_id, _ = _session_ids(client)
    response = client.post(
        f"/api/v1/orgs/{organization_id}/templates/generate-draft",
        json={"prompt": "Write a concise introduction for a prospect"},
    )

    assert response.status_code == 422
    assert "Gemini API key" in response.json()["detail"]


def test_generate_email_template_draft_uses_personal_groq_key(
    client,
    monkeypatch,
):
    organization_id, user_id = _session_ids(client)
    endpoint = f"/api/v1/orgs/{organization_id}/templates"
    api_key = "test-groq-api-key"
    saved_settings = client.put(
        "/api/v1/settings",
        json={"groq_api_key": api_key},
    )
    assert saved_settings.status_code == 200

    async def fake_generate(prompt, received_key, *, model=None):
        assert prompt == "Write a friendly introductory message"
        assert received_key == api_key
        assert model == "openai/gpt-oss-20b"
        return email_templates_api.GroqEmailTemplateDraftResponse(
            name="Friendly introduction",
            subject="A quick introduction",
            body_content="<p>Hello {{ contact.first_name }}</p>",
            model=model,
            usage={
                "prompt_tokens": 40,
                "completion_tokens": 20,
                "total_tokens": 60,
            },
        )

    monkeypatch.setattr(
        email_templates_api,
        "generate_groq_template_draft",
        fake_generate,
    )
    response = client.post(
        f"{endpoint}/generate-draft",
        json={
            "prompt": "Write a friendly introductory message",
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "name": "Friendly introduction",
        "subject": "A quick introduction",
        "body_content": "<p>Hello {{ contact.first_name }}</p>",
        "model": "openai/gpt-oss-20b",
        "usage": {
            "prompt_tokens": 40,
            "completion_tokens": 20,
            "total_tokens": 60,
        },
    }
    assert client.get(endpoint).json() == []
    with TestingSessionLocal() as db:
        context = db.info["tenant_context"]
        assert context.user_id == user_id


def test_generate_email_template_groq_requires_personal_key(client):
    organization_id, _ = _session_ids(client)
    response = client.post(
        f"/api/v1/orgs/{organization_id}/templates/generate-draft",
        json={
            "prompt": "Write a concise introduction for a prospect",
            "provider": "groq",
        },
    )

    assert response.status_code == 422
    assert "Groq API key" in response.json()["detail"]


def test_groq_model_list_uses_personal_key(client, monkeypatch):
    api_key = "test-groq-models-key"
    saved_settings = client.put(
        "/api/v1/settings",
        json={"groq_api_key": api_key},
    )
    assert saved_settings.status_code == 200

    async def fake_list_models(received_key):
        assert received_key == api_key
        return ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"]

    monkeypatch.setattr(
        settings_api,
        "list_available_draft_models",
        fake_list_models,
    )
    response = client.get("/api/v1/settings/groq/models")

    assert response.status_code == 200
    assert response.json() == {
        "models": ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"],
    }


def test_groq_model_list_requires_personal_key(client):
    response = client.get("/api/v1/settings/groq/models")

    assert response.status_code == 422
    assert "Groq API key" in response.json()["detail"]


def test_email_template_preview_builds_context_and_escapes_values(client):
    organization_id, _ = _session_ids(client)
    company_id, contact_id = _create_company_with_contact(organization_id)
    endpoint = f"/api/v1/orgs/{organization_id}/templates"
    created = client.post(
        endpoint,
        json={
            "name": "Personalized introduction",
            "subject": "Hello {{ contact.first_name | default('there') }}",
            "body_content": (
                "<p>Hello {{ contact.first_name | default('there') }}</p>"
                "<p>{{ company.status }} · {{ company.phone }}</p>"
                "<p>{{ company.name }}</p>"
                "<div>{{ company.crawled_text }}</div>"
                "<p>{{ promo_code }}</p>"
            ),
        },
    )
    assert created.status_code == 201

    preview = client.post(
        f"{endpoint}/{created.json()['id']}/preview",
        json={
            "company_id": str(company_id),
            "contact_id": str(contact_id),
            "custom_variables": {
                "company": {"name": "<strong>Custom name</strong>"},
                "promo_code": "WELCOME10",
            },
        },
    )
    assert preview.status_code == 200
    assert preview.json()["subject"] == "Hello there"
    assert "&lt;strong&gt;Custom name&lt;/strong&gt;" in preview.json()[
        "body_content"
    ]
    assert "qualified · +1 555 0100" in preview.json()["body_content"]
    assert "We build useful products." in preview.json()["body_content"]
    assert "WELCOME10" in preview.json()["body_content"]

    overridden_sender = client.post(
        f"{endpoint}/{created.json()['id']}/preview",
        json={
            "company_id": str(company_id),
            "contact_id": str(contact_id),
            "custom_variables": {"sender": {"email": "spoof@example.test"}},
        },
    )
    assert overridden_sender.status_code == 422

    unsafe_template = client.post(
        endpoint,
        json={
            "name": "Sandbox check",
            "subject": "Hello",
            "body_content": "{{ company.__class__ }}",
        },
    )
    assert unsafe_template.status_code == 201
    unsafe_preview = client.post(
        f"{endpoint}/{unsafe_template.json()['id']}/preview",
        json={"company_id": str(company_id)},
    )
    assert unsafe_preview.status_code == 422

    unsafe_filter = client.post(
        endpoint,
        json={
            "name": "Unsafe filter",
            "subject": "Hello",
            "body_content": "{{ company.name | safe }}",
        },
    )
    assert unsafe_filter.status_code == 422

    invalid_contact = client.post(
        f"{endpoint}/{created.json()['id']}/preview",
        json={
            "company_id": str(company_id),
            "contact_id": str(uuid4()),
        },
    )
    assert invalid_contact.status_code == 404


def test_email_template_send_uses_assigned_gmail_and_contact(client, monkeypatch):
    organization_id, admin_user_id = _session_ids(client)
    company_id, contact_id = _create_company_with_contact(organization_id)
    endpoint = f"/api/v1/orgs/{organization_id}/templates"
    created = client.post(
        endpoint,
        json={
            "name": "Follow up",
            "subject": "A note for {{ contact.first_name | default('you') }}",
            "body_content": (
                "<p>Hello {{ contact.first_name | default('there') }},</p>"
                "<p>{{ sender.email }} · {{ company.domain }}</p>"
            ),
        },
    )
    assert created.status_code == 201

    encryption_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(settings, "gmail_token_encryption_key", encryption_key)
    monkeypatch.setattr(settings, "google_oauth_client_id", "client-id")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "client-secret")
    with TestingSessionLocal() as db:
        account = ConnectedGmailAccount(
            org_id=organization_id,
            email_address="team@example.test",
            google_account_id="google-team-account",
            encrypted_refresh_token=gmail.encrypt_refresh_token("refresh-token"),
            token_updated_at=datetime.now(timezone.utc),
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        gmail_account_id = account.id

    send_endpoint = f"{endpoint}/{created.json()['id']}/send"
    request_payload = {
        "company_id": str(company_id),
        "contact_id": str(contact_id),
        "gmail_account_id": str(gmail_account_id),
    }
    unauthorized = client.post(send_endpoint, json=request_payload)
    assert unauthorized.status_code == 403

    with TestingSessionLocal() as db:
        db.add(
            UserGmailAssignment(
                org_id=organization_id,
                user_id=admin_user_id,
                gmail_account_id=gmail_account_id,
                assigned_by=admin_user_id,
            )
        )
        db.commit()

    captured = {}

    class FakeRequest:
        def execute(self):
            return {"id": "sent-message-id", "threadId": "sent-thread-id"}

    class FakeMessages:
        def send(self, *, userId, body):
            assert userId == "me"
            raw = body["raw"]
            raw += "=" * (-len(raw) % 4)
            message = BytesParser(policy=policy.default).parsebytes(
                base64.urlsafe_b64decode(raw)
            )
            captured["to"] = message["To"]
            captured["from"] = message["From"]
            captured["subject"] = message["Subject"]
            captured["html_body"] = message.get_body(
                preferencelist=("html",)
            ).get_content()
            return FakeRequest()

    class FakeUsers:
        def messages(self):
            return FakeMessages()

    class FakeService:
        def users(self):
            return FakeUsers()

    monkeypatch.setattr(gmail, "build", lambda *_args, **_kwargs: FakeService())
    sent = client.post(send_endpoint, json=request_payload)
    assert sent.status_code == 200
    assert sent.json()["status"] == "sent"
    assert sent.json()["message_id"] == "sent-message-id"
    assert sent.json()["recipient_email"] == "jane@example.test"
    assert captured["to"] == "jane@example.test"
    assert captured["from"] == "team@example.test"
    assert captured["subject"] == "A note for you"
    assert "team@example.test · example.test" in captured["html_body"]
