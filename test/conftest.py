
import sys
from pathlib import Path

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.api.deps import get_db
from app.main import app
from app import models  # Ensure models are registered


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


@pytest.fixture
def client(monkeypatch):
    Base.metadata.create_all(bind=test_engine)
    monkeypatch.setattr("app.main.should_auto_migrate", lambda: False)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=test_engine)
