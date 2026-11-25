import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from app.services import groq_template_generator as generator


def test_groq_generator_parses_draft_and_sends_api_key(monkeypatch):
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
                "model": generator.GROQ_MODEL,
                "usage": {
                    "prompt_tokens": 44,
                    "completion_tokens": 18,
                    "total_tokens": 62,
                },
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(draft),
                        },
                    },
                ],
            },
            headers={
                "x-ratelimit-remaining-requests": "21",
                "x-ratelimit-limit-requests": "30",
                "x-ratelimit-remaining-tokens": "9000",
                "x-ratelimit-limit-tokens": "12000",
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
            "test-groq-api-key",
        )
    )

    assert generated.model_dump(exclude_none=True) == {
        **draft,
        "model": generator.GROQ_MODEL,
        "usage": {
            "prompt_tokens": 44,
            "completion_tokens": 18,
            "total_tokens": 62,
            "requests_remaining": 21,
            "requests_limit": 30,
            "tokens_remaining": 9000,
            "tokens_limit": 12000,
        },
    }
    request = observed["request"]
    assert request.url == generator.GROQ_API_URL
    assert request.headers["authorization"] == "Bearer test-groq-api-key"
    payload = json.loads(request.content)
    assert payload["model"] == generator.GROQ_MODEL
    assert payload["response_format"] == generator.EMAIL_TEMPLATE_RESPONSE_FORMAT
    assert payload["messages"][-1] == {
        "role": "user",
        "content": "Write a friendly introduction",
    }
    assert "contact.first_name" in payload["messages"][0]["content"]


def test_groq_generator_redacts_api_key_from_provider_errors(monkeypatch):
    api_key = "test-groq-api-key"

    def handle_request(_request):
        return httpx.Response(
            400,
            json={"error": {"message": f"Invalid key {api_key}"}},
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
    assert "[redacted]" in exc_info.value.detail
    assert api_key not in exc_info.value.detail


def test_groq_generator_rejects_unsupported_template_placeholders(monkeypatch):
    draft = {
        "name": "Introduction",
        "subject": "Hello {{ contact.first_name }}",
        "body_content": "<p>{{ company.private_notes }}</p>",
    }

    def handle_request(_request):
        return httpx.Response(
            200,
            json={
                "model": generator.GROQ_MODEL,
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 11,
                    "total_tokens": 21,
                },
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(draft),
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

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            generator.generate_email_template_draft(
                "Write a friendly introduction",
                "test-groq-api-key",
            )
        )

    assert exc_info.value.status_code == 502
    assert "invalid template syntax or placeholders" in exc_info.value.detail


def test_groq_generator_uses_selected_model_and_extracts_usage(monkeypatch):
    observed = {}
    draft = {
        "name": "Friendly introduction",
        "subject": "Hello",
        "body_content": "<p>Hello</p>",
    }

    def handle_request(request):
        observed["request"] = request
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-oss-20b",
                "usage": {
                    "prompt_tokens": 35,
                    "completion_tokens": 19,
                    "total_tokens": 54,
                },
                "choices": [
                    {"message": {"content": json.dumps(draft)}},
                ],
            },
        )

    transport = httpx.MockTransport(handle_request)
    original_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        generator.httpx,
        "AsyncClient",
        lambda **kwargs: original_async_client(transport=transport, **kwargs),
    )

    result = asyncio.run(
        generator.generate_email_template_draft(
            "Write a friendly introduction",
            "test-groq-api-key",
            model="openai/gpt-oss-20b",
        )
    )

    payload = json.loads(observed["request"].content)
    assert payload["model"] == "openai/gpt-oss-20b"
    assert result.model == "openai/gpt-oss-20b"
    assert result.usage.prompt_tokens == 35
    assert result.usage.completion_tokens == 19
    assert result.usage.total_tokens == 54


def test_groq_model_list_returns_only_strict_output_models(monkeypatch):
    observed = {}

    def handle_request(request):
        observed["request"] = request
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "whisper-large-v3"},
                    {"id": "openai/gpt-oss-20b"},
                    {"id": "llama-3.3-70b-versatile"},
                    {"id": "canopylabs/orpheus-v1-english"},
                    {"id": "qwen/qwen3.8-27b"},
                    {"id": "meta-models/muse-spark-open"},
                ],
            },
        )

    transport = httpx.MockTransport(handle_request)
    original_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        generator.httpx,
        "AsyncClient",
        lambda **kwargs: original_async_client(transport=transport, **kwargs),
    )

    models = asyncio.run(
        generator.list_available_draft_models("test-groq-api-key")
    )

    assert models == [
        "meta-models/muse-spark-open",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
    ]
    assert observed["request"].url == generator.GROQ_MODELS_URL
    assert (
        observed["request"].headers["authorization"]
        == "Bearer test-groq-api-key"
    )


def test_groq_generator_rejects_models_without_strict_output_support():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            generator.generate_email_template_draft(
                "Write a friendly introduction",
                "test-groq-api-key",
                model="llama-3.3-70b-versatile",
            )
        )

    assert exc_info.value.status_code == 422
    assert "strict structured outputs" in exc_info.value.detail
