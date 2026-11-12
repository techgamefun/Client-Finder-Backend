import asyncio
import json as jsonlib

import httpx
from cryptography.fernet import Fernet

from app.config import settings
from app.services import octoparse


def test_octoparse_template_search_is_public_and_normalizes_templates(
    client,
    monkeypatch,
):
    monkeypatch.setattr(settings, "octoparse_api_key", "test-octoparse-key")

    class FakeAsyncClient:
        def __init__(self, timeout: float):
            assert timeout == octoparse.OCTOPARSE_REQUEST_TIMEOUT

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str, params: dict, headers: dict):
            assert url.endswith("/api/agentTools/searchTemplates")
            assert params == {
                "keyword": "business listing",
                "page": 1,
                "limit": 20,
            }
            assert headers == {"x-api-key": "test-octoparse-key"}
            return httpx.Response(
                200,
                json={
                    "data": {
                        "recommendedTemplateName": "directory_search",
                        "templates": [
                            {
                                "templateName": "directory_search",
                                "slug": "directory-search",
                                "executionMode": "cloud",
                                "inputSchema": [{"field": "search_keyword"}],
                                "outputSchema": [
                                    {"field": "business_name"},
                                    {"field": "profile_url"},
                                ],
                            }
                        ],
                    }
                },
            )

    monkeypatch.setattr(octoparse.httpx, "AsyncClient", FakeAsyncClient)

    response = client.get(
        "/api/v1/settings/octoparse/templates",
        params={"keyword": "business listing"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "recommended_template_name": "directory_search",
        "templates": [
            {
                "template_name": "directory_search",
                "slug": "directory-search",
                "execution_mode": "cloud",
                "description": None,
                "input_schema": [{"field": "search_keyword"}],
                "output_schema": [
                    {"field": "business_name"},
                    {"field": "profile_url"},
                ],
                "source_tree": None,
                "source_summary": None,
            }
        ],
    }


def test_octoparse_search_starts_and_polls_configured_template(
    client,
    monkeypatch,
):
    encryption_key = Fernet.generate_key().decode("ascii")
    octoparse_key = "test-octoparse-key"
    monkeypatch.setattr(settings, "settings_encryption_key", encryption_key)
    monkeypatch.setattr(settings, "octoparse_api_key", None)
    monkeypatch.setattr(settings, "tavily_api_key", None)

    configured = client.put(
        "/api/v1/settings",
        json={
            "octoparse_api_key": octoparse_key,
            "selected_discovery_tools": ["octoparse"],
            "octoparse_template": {
                "template_name": "directory_search",
                "query_field": "search_keyword",
                "additional_parameters": {"country": "US"},
                "title_field": "business_name",
                "url_field": "profile_url",
                "content_field": "description",
                "score_field": None,
            },
        },
    )
    assert configured.status_code == 200

    class FakeAsyncClient:
        def __init__(self, timeout: float):
            assert timeout == octoparse.OCTOPARSE_REQUEST_TIMEOUT

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, json: dict, headers: dict):
            assert url.endswith("/api/agentTools/executeTask")
            assert headers == {
                "x-api-key": octoparse_key,
                "x-external-user-id": octoparse.OCTOPARSE_EXTERNAL_USER_ID,
            }
            assert json["templateName"] == "directory_search"
            assert json["maxCollectedRows"] == 5
            assert jsonlib.loads(json["parameters"]) == {
                "country": "US",
                "search_keyword": "bike shops in Austin",
            }
            return httpx.Response(
                200,
                json={
                    "data": {
                        "success": True,
                        "status": "accepted",
                        "taskId": "octo-task-123",
                        "lotNo": "run-1",
                    }
                },
            )

        async def get(self, url: str, headers: dict):
            assert url.startswith(
                f"{octoparse.OCTOPARSE_API_BASE_URL}/api/agentTools/exportData?"
            )
            assert "taskId=octo-task-123" in url
            assert "lotNo=run-1" in url
            assert headers == {"x-api-key": octoparse_key}
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "exported",
                        "dataTotal": 1,
                        "sampleData": [
                            {
                                "business_name": "Austin Bike Shop",
                                "profile_url": "https://directory.example/austin-bike-shop",
                                "description": "Independent bicycle retailer",
                            }
                        ],
                    }
                },
            )

    monkeypatch.setattr(octoparse.httpx, "AsyncClient", FakeAsyncClient)

    started = client.post(
        "/api/v1/discovery/search",
        json={"query": "bike shops in Austin", "max_results": 5},
    )
    assert started.status_code == 200
    started_data = started.json()
    assert started_data["results"] == []
    assert started_data["provider_failures"] == []
    assert started_data["provider_jobs"][0]["provider"] == "octoparse"
    assert started_data["provider_jobs"][0]["status"] == "running"

    job_id = started_data["provider_jobs"][0]["id"]
    finished = client.get(f"/api/v1/discovery/jobs/{job_id}")
    assert finished.status_code == 200
    assert finished.json()["status"] == "succeeded"
    assert finished.json()["results"] == [
        {
            "title": "Austin Bike Shop",
            "url": "https://directory.example/austin-bike-shop",
            "content": "Independent bicycle retailer",
            "score": 0.5,
            "provider": "octoparse",
        }
    ]
