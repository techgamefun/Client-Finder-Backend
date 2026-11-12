import asyncio
import json

from cryptography.fernet import Fernet
import httpx

from app.config import settings
from app import worker as website_crawl_worker
from app.models import ApplicationSettings, WebsiteCrawlJob
from app.services import discovery
from app.services import website_crawler
from app.services.website_crawler import CrawlData
from conftest import TestingSessionLocal


ADMIN_TOKEN = "test-settings-admin-token"
TAVILY_API_KEY = "tvly-test-secret"


def test_settings_api_requires_admin_token(client, monkeypatch):
    monkeypatch.setattr(settings, "settings_admin_token", ADMIN_TOKEN)

    response = client.get("/api/v1/settings")
    assert response.status_code == 401

    response = client.get(
        "/api/v1/settings",
        headers={"Authorization": "Bearer incorrect"},
    )
    assert response.status_code == 401


def test_settings_save_encrypts_key_and_returns_usage(client, monkeypatch):
    encryption_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(settings, "settings_admin_token", ADMIN_TOKEN)
    monkeypatch.setattr(settings, "settings_encryption_key", encryption_key)
    monkeypatch.setattr(settings, "tavily_api_key", None)
    request_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    saved = client.put(
        "/api/v1/settings",
        headers=request_headers,
        json={
            "crawl_max_pages": 4,
            "crawl_max_depth": 1,
            "tavily_api_key": TAVILY_API_KEY,
        },
    )

    assert saved.status_code == 200
    assert saved.json() == {
        "crawl_max_pages": 4,
        "crawl_max_depth": 1,
        "tavily_configured": True,
        "tavily_key_source": "database",
    }

    with TestingSessionLocal() as db:
        row = db.get(ApplicationSettings, 1)
        assert row is not None
        assert row.tavily_api_key_encrypted != TAVILY_API_KEY
        assert (
            Fernet(encryption_key.encode("ascii"))
            .decrypt(row.tavily_api_key_encrypted.encode("ascii"))
            .decode("utf-8")
            == TAVILY_API_KEY
        )

    class FakeAsyncClient:
        def __init__(self, timeout: float):
            assert timeout == 20.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str, headers: dict[str, str]):
            assert url == discovery.TAVILY_USAGE_URL
            assert headers == {
                "Authorization": f"Bearer {TAVILY_API_KEY}",
            }
            return httpx.Response(
                200,
                json={
                    "key": {
                        "usage": 123,
                        "limit": 1000,
                        "search_usage": 100,
                    },
                    "account": {
                        "current_plan": "Researcher",
                        "plan_usage": 123,
                        "plan_limit": 1000,
                    },
                },
            )

    monkeypatch.setattr(discovery.httpx, "AsyncClient", FakeAsyncClient)
    usage = client.get(
        "/api/v1/settings/tavily/usage",
        headers=request_headers,
    )
    assert usage.status_code == 200
    assert usage.json()["key"]["usage"] == 123
    assert usage.json()["key"]["limit"] == 1000
    assert usage.json()["account"]["current_plan"] == "Researcher"


def test_worker_applies_saved_crawl_limits(client, monkeypatch):
    received_options = {}

    async def fake_crawl(_website_url: str, **options):
        received_options.update(options)
        return CrawlData(
            website_url="https://example.com/",
            about_page_url=None,
            about_content=None,
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
        )

    monkeypatch.setattr(
        website_crawl_worker,
        "SessionLocal",
        TestingSessionLocal,
    )
    monkeypatch.setattr(website_crawl_worker, "crawl_website", fake_crawl)
    with TestingSessionLocal() as db:
        db.add(
            ApplicationSettings(
                id=1,
                crawl_max_pages=3,
                crawl_max_depth=0,
            )
        )
        job = WebsiteCrawlJob(website_url="https://example.com/")
        db.add(job)
        db.commit()
        job_id = job.id

    asyncio.run(website_crawl_worker.process_job(job_id))

    assert received_options == {"max_pages": 3, "max_depth": 0}


def test_crawl_request_sends_saved_limits_to_crawler_service(monkeypatch):
    received_payload = {}
    response_body = {
        "website_url": "https://example.com/",
        "about_page_url": None,
        "about_content": None,
        "contact_page_url": None,
        "contact_content": None,
        "goals_page_url": None,
        "goals_content": None,
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        received_payload.update(json.loads(request.content))
        return httpx.Response(200, json=response_body)

    real_async_client = httpx.AsyncClient

    def mock_async_client(**kwargs):
        return real_async_client(
            transport=httpx.MockTransport(handle_request),
            **kwargs,
        )

    monkeypatch.setattr(
        website_crawler.httpx,
        "AsyncClient",
        mock_async_client,
    )
    monkeypatch.setattr(
        website_crawler.settings,
        "crawler_service_url",
        "http://crawler.internal:8001",
    )

    asyncio.run(
        website_crawler.crawl_website(
            "https://example.com/",
            max_pages=4,
            max_depth=1,
        )
    )

    assert received_payload == {
        "website_url": "https://example.com/",
        "max_pages": 4,
        "max_depth": 1,
    }
