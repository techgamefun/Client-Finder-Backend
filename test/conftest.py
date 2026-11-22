
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database import SessionLocal
from app.api.deps import get_db
from app.main import app
from app import models  # Ensure models are registered
from app.config import settings
from app.tenant_context import TenantContext


CRAWLER_SERVICE_PATH = Path(__file__).resolve().parents[2] / "crawler-service"
sys.path.insert(0, str(CRAWLER_SERVICE_PATH))


TEST_DATABASE_URL = "sqlite://"

test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestingSessionLocal = sessionmaker(
    bind=test_engine,
    autoflush=False,
    expire_on_commit=False,
)


def create_invited_member(
    admin_client: TestClient,
    *,
    email: str,
    password: str,
    role: str = "user",
) -> dict:
    invitation = admin_client.post(
        "/api/v1/auth/invitations",
        json={"email": email, "role": role},
    )
    assert invitation.status_code == 201
    token = parse_qs(urlparse(invitation.json()["invitation_url"]).query)["token"][0]
    with TestClient(app) as invitee:
        accepted = invitee.post(
            f"/api/v1/auth/invitations/{token}/accept",
            json={
                "first_name": "Test",
                "last_name": "Member",
                "password": password,
            },
        )
    assert accepted.status_code == 200
    return accepted.json()


@pytest.fixture
def client(monkeypatch):
    Base.metadata.create_all(bind=test_engine)
    monkeypatch.setattr("app.main.should_auto_migrate", lambda: False)
    monkeypatch.setattr(settings, "access_token_secret", "11" * 32)
    monkeypatch.setattr(settings, "refresh_token_secret", "22" * 32)
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings, "run_worker_in_api", False)
    monkeypatch.setattr(settings, "tavily_api_key", None)
    monkeypatch.setattr(settings, "octoparse_api_key", None)
    monkeypatch.setattr(settings, "google_pagespeed_api_key", None)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = TestingSessionLocal

    with TestClient(app) as test_client:
        bootstrap_response = test_client.post(
            "/api/v1/auth/bootstrap",
            json={
                "organization_name": "Test Organization",
                "email": "admin@example.test",
                "password": "test-password-long-enough",
                "first_name": "Test",
                "last_name": "Admin",
            },
        )
        assert bootstrap_response.status_code == 200
        session = bootstrap_response.json()
        TestingSessionLocal.configure(
            info={
                "tenant_context": TenantContext(
                    organization_id=UUID(session["organization_id"]),
                    user_id=UUID(session["user"]["id"]),
                    email=session["user"]["email"],
                    role=session["user"]["role"],
                )
            }
        )
        yield test_client

    app.dependency_overrides.clear()
    app.state.session_factory = SessionLocal
    TestingSessionLocal.configure(info={})
    Base.metadata.drop_all(bind=test_engine)
