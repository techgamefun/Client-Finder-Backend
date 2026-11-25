import json
from dataclasses import dataclass
from typing import Literal

import httpx
from fastapi import HTTPException, status
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.http_clients import async_http_client
from app.services.gemini_template_generator import GEMINI_MODEL
from app.services.groq_template_generator import (
    GROQ_MODEL,
    STRUCTURED_OUTPUT_MODEL_IDS,
)


DiscoveryAiProvider = Literal["gemini", "groq"]
GEMINI_QUERY_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
GROQ_QUERY_URL = "https://api.groq.com/openai/v1/chat/completions"
QUERY_GENERATION_TIMEOUT_SECONDS = 30.0
MAX_QUERY_LENGTH = 300
MAX_GENERATED_SEARCH_KEYWORDS = 5


@dataclass(frozen=True)
class DiscoverySearchKeywordGeneration:
    keywords: list[str]
    provider_used: DiscoveryAiProvider
    fallback_reason: str | None = None


class GeneratedSearchKeywords(BaseModel):
    keywords: list[str] = Field(
        min_length=MAX_GENERATED_SEARCH_KEYWORDS,
        max_length=MAX_GENERATED_SEARCH_KEYWORDS,
    )

    @field_validator("keywords")
    @classmethod
    def trim_keywords(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values]


async def generate_discovery_search_keywords(
    prompt: str,
    provider: DiscoveryAiProvider,
    api_key: str,
    *,
    model: str | None = None,
    fallback_api_key: str | None = None,
) -> DiscoverySearchKeywordGeneration:
    provider_used = provider
    fallback_reason = None
    if provider == "gemini":
        try:
            generated_text = await _generate_with_gemini(prompt, api_key)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
                raise
            if not fallback_api_key:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail=(
                        f"{exc.detail} Groq fallback is unavailable because "
                        "no Groq API key is configured."
                    ),
                ) from exc
            fallback_reason = str(exc.detail)
            provider_used = "groq"
            try:
                generated_text = await _generate_with_groq(
                    prompt,
                    fallback_api_key,
                    model=GROQ_MODEL,
                )
            except HTTPException as fallback_exc:
                raise HTTPException(
                    status_code=fallback_exc.status_code,
                    detail=(
                        f"{exc.detail} Groq fallback failed: "
                        f"{fallback_exc.detail}"
                    ),
                ) from fallback_exc
    else:
        selected_model = model or GROQ_MODEL
        if selected_model not in STRUCTURED_OUTPUT_MODEL_IDS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Select a Groq model that supports strict structured "
                    "outputs."
                ),
            )
        generated_text = await _generate_with_groq(
            prompt,
            api_key,
            model=selected_model,
        )

    try:
        generated = GeneratedSearchKeywords.model_validate_json(generated_text)
    except ValidationError as exc:
        detail = (
            f"{provider_used.capitalize()} returned invalid search keywords."
        )
        if fallback_reason:
            detail = f"{fallback_reason} Groq fallback failed: {detail}"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        ) from exc

    keywords = generated.keywords
    if (
        any(
            len(keyword) < 3 or len(keyword) > MAX_QUERY_LENGTH
            for keyword in keywords
        )
        or len({keyword.casefold() for keyword in keywords})
        != MAX_GENERATED_SEARCH_KEYWORDS
    ):
        detail = (
            f"{provider_used.capitalize()} did not provide five distinct, "
            "useful search keywords."
        )
        if fallback_reason:
            detail = f"{fallback_reason} Groq fallback failed: {detail}"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        )
    return DiscoverySearchKeywordGeneration(
        keywords=keywords,
        provider_used=provider_used,
        fallback_reason=fallback_reason,
    )


async def _generate_with_gemini(prompt: str, api_key: str) -> str:
    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "Interpret the user's natural-language business "
                        "discovery request and create exactly five distinct, "
                        "concise search keyword phrases for finding matching "
                        "businesses or websites. Preserve the requested "
                        "business type, audience, location, and intent when "
                        "present. Do not invent details. Return only the "
                        "required JSON object."
                    ),
                },
            ],
        },
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "required": ["keywords"],
                "properties": {
                    "keywords": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                    },
                },
            },
            "maxOutputTokens": 512,
            "temperature": 0.4,
        },
    }
    response = await _post_provider(
        GEMINI_QUERY_URL,
        payload,
        headers={"x-goog-api-key": api_key},
        provider="Gemini",
        api_key=api_key,
    )
    try:
        response_data = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gemini returned an unreadable query response.",
        ) from exc
    text = _candidate_text(response_data)
    if text is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gemini did not return usable search keywords.",
        )
    return text


async def _generate_with_groq(
    prompt: str,
    api_key: str,
    *,
    model: str = GROQ_MODEL,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Interpret the user's natural-language business "
                    "discovery request and create exactly five distinct, "
                    "concise search keyword phrases for finding matching "
                    "businesses or websites. Preserve the requested business "
                    "type, audience, location, and intent when present. Do "
                    "not invent details. Return only the required JSON object."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "discovery_search_keywords",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["keywords"],
                    "additionalProperties": False,
                },
            },
        },
        "max_completion_tokens": 512,
        "temperature": 0.4,
    }
    response = await _post_provider(
        GROQ_QUERY_URL,
        payload,
        headers={"Authorization": f"Bearer {api_key}"},
        provider="Groq",
        api_key=api_key,
    )
    try:
        response_data = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq returned an unreadable query response.",
        ) from exc
    text = _completion_text(response_data)
    if text is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq did not return usable search keywords.",
        )
    return text


async def _post_provider(
    url: str,
    payload: dict[str, object],
    *,
    headers: dict[str, str],
    provider: str,
    api_key: str,
) -> httpx.Response:
    try:
        async with async_http_client(
            timeout=QUERY_GENERATION_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"{provider} took too long to expand the search keywords.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not connect to {provider} to expand search keywords.",
        ) from exc

    if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"{provider} rate limit reached. Try again shortly.",
        )
    if response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        detail = (
            f"{provider} query generation failed "
            "(HTTP 503): The provider is temporarily unavailable."
        )
        try:
            response_data = response.json()
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=detail,
            )
        error = (
            response_data.get("error")
            if isinstance(response_data, dict)
            else None
        )
        message = error.get("message") if isinstance(error, dict) else None
        safe_message = (
            message.strip().replace(api_key, "[redacted]")[:500]
            if isinstance(message, str) and message.strip()
            else ""
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{provider} query generation failed (HTTP 503): "
                f"{safe_message}"
                if safe_message
                else detail
            ),
        )
    if response.is_error:
        default = (
            f"{provider} query generation failed (HTTP {response.status_code}). "
            "Check the API key and model access."
        )
        try:
            response_data = response.json()
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=default,
            )
        error = (
            response_data.get("error")
            if isinstance(response_data, dict)
            else None
        )
        message = error.get("message") if isinstance(error, dict) else None
        safe_message = (
            message.strip().replace(api_key, "[redacted]")[:500]
            if isinstance(message, str) and message.strip()
            else ""
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"{provider} query generation failed "
                f"(HTTP {response.status_code}): {safe_message}"
                if safe_message
                else default
            ),
        )
    return response


def _candidate_text(response_data: object) -> str | None:
    if not isinstance(response_data, dict):
        return None
    candidates = response_data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return None
    content = candidate.get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    text = "".join(
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )
    return text or None


def _completion_text(response_data: object) -> str | None:
    if not isinstance(response_data, dict):
        return None
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    message = choice.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) and content else None
