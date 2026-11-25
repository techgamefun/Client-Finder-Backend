import json

import httpx
from fastapi import HTTPException, status

from app.schemas.email_templates import (
    EmailTemplateDraftResponse,
)
from app.http_clients import async_http_client
from app.services.email_template_draft_validation import (
    build_email_template_system_prompt,
    parse_email_template_draft,
    validate_template_variables as _validate_template_variables,
)


GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
GEMINI_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_TOKENS = 2_048


async def generate_email_template_draft(
    prompt: str,
    api_key: str,
) -> EmailTemplateDraftResponse:
    request_payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": build_email_template_system_prompt(),
                },
            ],
        },
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "required": ["name", "subject", "body_content"],
                "properties": {
                    "name": {"type": "STRING"},
                    "subject": {"type": "STRING"},
                    "body_content": {"type": "STRING"},
                },
            },
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.6,
        },
    }

    try:
        async with async_http_client(
            timeout=GEMINI_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(
                GEMINI_API_URL,
                headers={"x-goog-api-key": api_key},
                json=request_payload,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Gemini took too long to generate a draft. Try again.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not connect to Gemini. Try again later.",
        ) from exc

    if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Gemini rate limit reached. Try again shortly.",
        )
    if response.is_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_provider_error_message(response, api_key),
        )

    try:
        response_data = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gemini returned an unreadable response.",
        ) from exc

    generated_text = _candidate_text(response_data)
    if generated_text is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Gemini did not return a usable draft. Try revising your "
                "description."
            ),
        )

    return parse_email_template_draft(generated_text, "Gemini")


def _provider_error_message(response: httpx.Response, api_key: str) -> str:
    default_message = (
        f"Gemini request failed (HTTP {response.status_code}). Check the "
        "API key, model access, and request configuration."
    )
    try:
        response_data = response.json()
    except (json.JSONDecodeError, ValueError):
        return default_message

    if not isinstance(response_data, dict):
        return default_message
    error = response_data.get("error")
    if not isinstance(error, dict):
        return default_message
    message = error.get("message")
    if not isinstance(message, str) or not message.strip():
        return default_message

    safe_message = message.strip().replace(api_key, "[redacted]")
    return (
        f"Gemini request failed (HTTP {response.status_code}): "
        f"{safe_message[:500]}"
    )


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


