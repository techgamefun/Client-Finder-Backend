import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app.services import discovery_query_generator as generator


def test_keyword_generation_uses_prompt_and_returns_only_generated_keywords(
    monkeypatch,
):
    received: dict[str, str] = {}

    async def fake_generate(prompt, _api_key):
        received["prompt"] = prompt
        return (
            '{"keywords":["local SaaS companies Austin",'
            '"software startups in Austin",'
            '"B2B software vendors Austin",'
            '"Austin software product companies",'
            '"SaaS providers serving Austin businesses"]}'
        )

    monkeypatch.setattr(generator, "_generate_with_gemini", fake_generate)

    generation = asyncio.run(
        generator.generate_discovery_search_keywords(
            "Find B2B software startups in Austin",
            "gemini",
            "test-key",
        ),
    )

    assert received["prompt"] == "Find B2B software startups in Austin"
    assert generation.keywords == [
        "local SaaS companies Austin",
        "software startups in Austin",
        "B2B software vendors Austin",
        "Austin software product companies",
        "SaaS providers serving Austin businesses",
    ]
    assert generation.provider_used == "gemini"
    assert generation.fallback_reason is None


def test_gemini_503_falls_back_to_groq_and_returns_reason(monkeypatch):
    received: dict[str, str] = {}

    async def fail_with_503(_prompt, _api_key):
        raise HTTPException(
            status_code=503,
            detail=(
                "Gemini query generation failed (HTTP 503): "
                "This model is currently experiencing high demand."
            ),
        )

    async def fake_groq(prompt, api_key, *, model):
        received.update(prompt=prompt, api_key=api_key, model=model)
        return (
            '{"keywords":["local SaaS companies Austin",'
            '"software startups in Austin",'
            '"B2B software vendors Austin",'
            '"Austin software product companies",'
            '"SaaS providers serving Austin businesses"]}'
        )

    monkeypatch.setattr(generator, "_generate_with_gemini", fail_with_503)
    monkeypatch.setattr(generator, "_generate_with_groq", fake_groq)

    generation = asyncio.run(
        generator.generate_discovery_search_keywords(
            "Find B2B software startups in Austin",
            "gemini",
            "gemini-key",
            fallback_api_key="groq-key",
        ),
    )

    assert received == {
        "prompt": "Find B2B software startups in Austin",
        "api_key": "groq-key",
        "model": generator.GROQ_MODEL,
    }
    assert generation.provider_used == "groq"
    assert generation.fallback_reason == (
        "Gemini query generation failed (HTTP 503): "
        "This model is currently experiencing high demand."
    )


def test_gemini_non_503_error_does_not_use_fallback(monkeypatch):
    async def fail_with_502(_prompt, _api_key):
        raise HTTPException(status_code=502, detail="Invalid Gemini response.")

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Groq fallback should only follow Gemini HTTP 503")

    monkeypatch.setattr(generator, "_generate_with_gemini", fail_with_502)
    monkeypatch.setattr(generator, "_generate_with_groq", fail_if_called)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            generator.generate_discovery_search_keywords(
                "Find B2B software startups in Austin",
                "gemini",
                "gemini-key",
                fallback_api_key="groq-key",
            ),
        )

    assert error.value.status_code == 502


def test_provider_503_preserves_reason_and_redacts_api_key(monkeypatch):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, json):
            assert headers["x-goog-api-key"] == "gemini-secret"
            return httpx.Response(
                503,
                request=httpx.Request("POST", url),
                json={
                    "error": {
                        "message": (
                            "This model is overloaded; key gemini-secret "
                            "should not appear."
                        ),
                    },
                },
            )

    monkeypatch.setattr(
        generator,
        "async_http_client",
        lambda **_kwargs: FakeClient(),
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            generator._post_provider(
                "https://example.test/generate",
                {},
                headers={"x-goog-api-key": "gemini-secret"},
                provider="Gemini",
                api_key="gemini-secret",
            ),
        )

    assert error.value.status_code == 503
    assert "This model is overloaded" in error.value.detail
    assert "gemini-secret" not in error.value.detail
    assert "[redacted]" in error.value.detail


def test_keyword_generation_rejects_duplicate_keywords(monkeypatch):
    async def fake_generate(_prompt, _api_key, *, model):
        assert model == generator.GROQ_MODEL
        return (
            '{"keywords":["software startups Austin",'
            '"Software Startups Austin",'
            '"software companies Austin",'
            '"IT services Austin",'
            '"IT companies Austin"]}'
        )

    monkeypatch.setattr(generator, "_generate_with_groq", fake_generate)

    with pytest.raises(HTTPException, match="five distinct"):
        asyncio.run(
            generator.generate_discovery_search_keywords(
                "Find software startups in Austin",
                "groq",
                "test-key",
            ),
        )


def test_keyword_generation_uses_selected_groq_model(monkeypatch):
    received: dict[str, str] = {}

    async def fake_generate(_prompt, _api_key, *, model):
        received["model"] = model
        return (
            '{"keywords":["managed IT companies Denver",'
            '"IT providers Denver",'
            '"Denver technology support companies",'
            '"Denver IT consultants",'
            '"small business IT firms Denver"]}'
        )

    monkeypatch.setattr(generator, "_generate_with_groq", fake_generate)

    generation = asyncio.run(
        generator.generate_discovery_search_keywords(
            "Find IT companies in Denver",
            "groq",
            "test-key",
            model="openai/gpt-oss-120b",
        ),
    )

    assert received["model"] == "openai/gpt-oss-120b"
    assert generation.keywords == [
        "managed IT companies Denver",
        "IT providers Denver",
        "Denver technology support companies",
        "Denver IT consultants",
        "small business IT firms Denver",
    ]
    assert generation.provider_used == "groq"


def test_keyword_generation_rejects_unsupported_groq_model(monkeypatch):
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Groq must not be called for unsupported models")

    monkeypatch.setattr(generator, "_generate_with_groq", fail_if_called)

    with pytest.raises(HTTPException, match="supports strict structured"):
        asyncio.run(
            generator.generate_discovery_search_keywords(
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
                                '{"keywords":["local SaaS companies Austin",'
                                '"Austin software startups",'
                                '"B2B software vendors Austin",'
                                '"Austin software product companies",'
                                '"SaaS providers serving Austin businesses"]}'
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
    assert "keywords" in payload["response_format"]["json_schema"]["schema"]["required"]
