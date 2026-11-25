import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app.services import discovery_query_generator as generator


def test_query_generation_keeps_original_and_deduplicates_expansions(
    monkeypatch,
):
    async def fake_generate(_keywords, _api_key):
        return (
            '{"queries":["B2B software companies in Austin",'
            '"SaaS businesses Austin Texas"]}'
        )

    monkeypatch.setattr(generator, "_generate_with_gemini", fake_generate)

    queries = asyncio.run(
        generator.generate_discovery_search_queries(
            "B2B software companies in Austin",
            "gemini",
            "test-key",
        ),
    )

    assert queries == [
        "B2B software companies in Austin",
        "SaaS businesses Austin Texas",
    ]


def test_query_generation_rejects_unusable_expansions(monkeypatch):
    async def fake_generate(_keywords, _api_key, *, model):
        assert model == generator.GROQ_MODEL
        return '{"queries":["B2B software companies in Austin"," "]}'

    monkeypatch.setattr(generator, "_generate_with_groq", fake_generate)

    with pytest.raises(HTTPException, match="useful query expansion"):
        asyncio.run(
            generator.generate_discovery_search_queries(
                "B2B software companies in Austin",
                "groq",
                "test-key",
            ),
        )


def test_query_generation_uses_selected_groq_model(monkeypatch):
    received: dict[str, str] = {}

    async def fake_generate(_keywords, _api_key, *, model):
        received["model"] = model
        return '{"queries":["managed IT companies Denver",""]}'

    monkeypatch.setattr(generator, "_generate_with_groq", fake_generate)

    queries = asyncio.run(
        generator.generate_discovery_search_queries(
            "IT companies in Denver",
            "groq",
            "test-key",
            model="openai/gpt-oss-120b",
        ),
    )

    assert received["model"] == "openai/gpt-oss-120b"
    assert queries == [
        "IT companies in Denver",
        "managed IT companies Denver",
    ]


def test_query_generation_rejects_unsupported_groq_model(monkeypatch):
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Groq must not be called for unsupported models")

    monkeypatch.setattr(generator, "_generate_with_groq", fail_if_called)

    with pytest.raises(HTTPException, match="supports strict structured"):
        asyncio.run(
            generator.generate_discovery_search_queries(
                "IT companies in Denver",
                "groq",
                "test-key",
                model="unsupported/model",
            ),
        )


def test_groq_query_generation_uses_strict_structured_output(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_post(url, payload, **kwargs):
        captured["url"] = url
        captured["payload"] = payload
        captured.update(kwargs)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"queries":["local SaaS companies Austin",'
                                '"Austin software startups"]}'
                            ),
                        },
                    },
                ],
            },
        )

    monkeypatch.setattr(generator, "_post_provider", fake_post)

    text = asyncio.run(
        generator._generate_with_groq(
            "SaaS companies in Austin",
            "test-key",
            model="openai/gpt-oss-120b",
        ),
    )

    assert '"local SaaS companies Austin"' in text
    assert captured["url"] == generator.GROQ_QUERY_URL
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "openai/gpt-oss-120b"
    assert payload["response_format"]["json_schema"]["strict"] is True
