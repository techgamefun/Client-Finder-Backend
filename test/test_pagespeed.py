import asyncio

import httpx
import pytest

from app.services import pagespeed


def test_pagespeed_scan_requests_all_categories_and_preserves_audits(
    monkeypatch,
):
    received: list[httpx.Request] = []
    response_body = {
        "analysisUTCTimestamp": "2026-11-09T12:00:00.000Z",
        "loadingExperience": {"overall_category": "FAST"},
        "originLoadingExperience": {"overall_category": "AVERAGE"},
        "lighthouseResult": {
            "finalUrl": "https://example.com/",
            "fetchTime": "2026-11-09T12:00:00.000Z",
            "lighthouseVersion": "12.0.0",
            "categories": {
                "performance": {"score": 0.91},
                "accessibility": {"score": 0.92},
                "best-practices": {"score": 0.93},
                "seo": {"score": 0.94},
            },
            "audits": {
                "first-contentful-paint": {
                    "title": "First Contentful Paint",
                    "score": 0.9,
                    "displayValue": "1.2 s",
                }
            },
        },
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json=response_body)

    real_async_client = httpx.AsyncClient

    def mock_async_client(**kwargs):
        return real_async_client(
            transport=httpx.MockTransport(handle_request),
            **kwargs,
        )

    async def allow_public_url(_: str) -> None:
        return None

    monkeypatch.setattr(pagespeed.httpx, "AsyncClient", mock_async_client)
    monkeypatch.setattr(pagespeed, "_validate_public_url", allow_public_url)
    monkeypatch.setattr(
        pagespeed.settings,
        "google_pagespeed_api_key",
        "test-key",
    )

    scan = asyncio.run(
        pagespeed.scan_pagespeed("https://example.com/", "mobile")
    )

    assert len(received) == 1
    assert received[0].url.params["strategy"] == "mobile"
    assert received[0].url.params.get_list("category") == [
        "performance",
        "accessibility",
        "best-practices",
        "seo",
    ]
    assert scan.category_scores == {
        "performance": 91,
        "accessibility": 92,
        "best-practices": 93,
        "seo": 94,
    }
    assert scan.report["loadingExperience"]["overall_category"] == "FAST"
    assert (
        scan.report["lighthouseResult"]["audits"]["first-contentful-paint"][
            "displayValue"
        ]
        == "1.2 s"
    )


def test_pagespeed_scan_requires_configured_key(monkeypatch):
    monkeypatch.setattr(
        pagespeed.settings,
        "google_pagespeed_api_key",
        None,
    )

    with pytest.raises(
        pagespeed.PageSpeedError,
        match="Google PageSpeed API key is not configured",
    ):
        asyncio.run(
            pagespeed.scan_pagespeed("https://example.com/", "mobile")
        )
