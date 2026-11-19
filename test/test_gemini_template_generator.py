import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from app.services import gemini_template_generator as generator


def test_gemini_generator_parses_draft_and_sends_api_key(monkeypatch):
    observed = {}
    draft = {
        "name": "Friendly introduction",
        "subject": "Hello {{ contact.first_name }}",
        "body_content": "<p>Hello {{ contact.first_name }}</p>",
    }

    def handle_request(request):
        observed["request"] = request
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": json.dumps(draft)}],
                        },
                    },
                ],
            },
        )

    transport = httpx.MockTransport(handle_request)
    original_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        generator.httpx,
        "AsyncClient",
        lambda **kwargs: original_async_client(
            transport=transport,
            **kwargs,
        ),
    )

    generated = asyncio.run(
        generator.generate_email_template_draft(
            "Write a friendly introduction",
            "test-gemini-api-key",
        )
    )

    assert generated.model_dump() == draft
    request = observed["request"]
    assert request.headers["x-goog-api-key"] == "test-gemini-api-key"
    assert request.headers["content-type"] == "application/json"
    assert request.url.path.endswith("/gemini-3.8-flash:generateContent")
    request_payload = json.loads(request.content)
    assert request_payload["contents"] == [
        {
            "role": "user",
            "parts": [{"text": "Write a friendly introduction"}],
        },
    ]
    assert request_payload["generationConfig"]["responseMimeType"] == (
        "application/json"
    )
    assert request_payload["generationConfig"]["responseSchema"]["required"] == [
        "name",
        "subject",
        "body_content",
    ]


def test_gemini_generator_surfaces_provider_error_without_exposing_api_key(
    monkeypatch,
):
    api_key = "test-gemini-api-key"

    def handle_request(_request):
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": f"Invalid request using {api_key}",
                },
            },
        )

    transport = httpx.MockTransport(handle_request)
    original_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        generator.httpx,
        "AsyncClient",
        lambda **kwargs: original_async_client(
            transport=transport,
            **kwargs,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            generator.generate_email_template_draft(
                "Write a friendly introduction",
                api_key,
            )
        )

    assert exc_info.value.status_code == 502
    assert "HTTP 400" in exc_info.value.detail
    assert "[redacted]" in exc_info.value.detail
    assert api_key not in exc_info.value.detail


def test_gemini_generator_rejects_unknown_nested_template_variables():
    with pytest.raises(HTTPException) as exc_info:
        generator._validate_template_variables(
            "Hello {{ contact.first_name }}",
            "<p>{{ company.private_notes }}</p>",
        )

    assert exc_info.value.status_code == 422
    assert generator._candidate_text({"candidates": [None]}) is None

    with pytest.raises(HTTPException) as exc_info:
        generator._validate_template_variables(
            "Hello",
            "<p>{{ company }}</p>",
        )

    assert exc_info.value.status_code == 422
