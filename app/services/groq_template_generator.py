import json

import httpx
from fastapi import HTTPException, status

from app.http_clients import async_http_client
from app.schemas.email_templates import (
    GroqEmailTemplateDraftResponse,
    GroqCompletionUsage,
)
from app.services.email_template_draft_validation import (
    build_email_template_system_prompt,
    parse_email_template_draft,
)


GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
GROQ_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_TOKENS = 2_048
STRUCTURED_OUTPUT_MODEL_IDS = frozenset(
    {
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
        "qwen/qwen3.8-27b",
        "meta-models/muse-spark-open",
    }
)
EMAIL_TEMPLATE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "email_template_draft",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "subject": {"type": "string"},
                "body_content": {"type": "string"},
            },
            "required": ["name", "subject", "body_content"],
            "additionalProperties": False,
        },
    },
}


async def generate_email_template_draft(
    prompt: str,
    api_key: str,
    *,
    model: str | None = None,
) -> GroqEmailTemplateDraftResponse:
    selected_model = model or GROQ_MODEL
    if selected_model not in STRUCTURED_OUTPUT_MODEL_IDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Select a Groq model that supports strict structured outputs."
            ),
        )
    request_payload = {
        "model": selected_model,
        "messages": [
            {
                "role": "system",
                "content": build_email_template_system_prompt(),
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": EMAIL_TEMPLATE_RESPONSE_FORMAT,
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.6,
    }

    try:
        async with async_http_client(timeout=GROQ_TIMEOUT_SECONDS) as client:
            response = await client.post(
                GROQ_API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json=request_payload,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Groq took too long to generate a draft. Try again.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not connect to Groq. Try again later.",
        ) from exc

    if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Groq rate limit reached. Try again shortly.",
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
            detail="Groq returned an unreadable response.",
        ) from exc

    generated_text = _completion_text(response_data)
    if generated_text is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Groq did not return a usable draft. Try revising your "
                "description."
            ),
        )
    draft = parse_email_template_draft(generated_text, "Groq")
    if not isinstance(response_data, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq returned an unreadable response.",
        )
    response_model = response_data.get("model")
    if not isinstance(response_model, str) or not response_model.strip():
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq did not return the model used for the draft.",
        )
    usage_data = response_data.get("usage")
    if not isinstance(usage_data, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq did not return usage statistics for the draft.",
        )
    try:
        usage = GroqCompletionUsage(
            prompt_tokens=usage_data["prompt_tokens"],
            completion_tokens=usage_data["completion_tokens"],
            total_tokens=usage_data["total_tokens"],
            requests_remaining=_response_header_int(
                response,
                "x-ratelimit-remaining-requests",
            ),
            requests_limit=_response_header_int(
                response,
                "x-ratelimit-limit-requests",
            ),
            tokens_remaining=_response_header_int(
                response,
                "x-ratelimit-remaining-tokens",
            ),
            tokens_limit=_response_header_int(
                response,
                "x-ratelimit-limit-tokens",
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq returned invalid usage statistics.",
        ) from exc

    return GroqEmailTemplateDraftResponse(
        name=draft.name,
        subject=draft.subject,
        body_content=draft.body_content,
        model=response_model,
        usage=usage,
    )


async def list_available_draft_models(api_key: str) -> list[str]:
    try:
        async with async_http_client(timeout=GROQ_TIMEOUT_SECONDS) as client:
            response = await client.get(
                GROQ_MODELS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Groq took too long to load available models. Try again.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not connect to Groq. Try again later.",
        ) from exc

    if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Groq rate limit reached. Try again shortly.",
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
            detail="Groq returned an unreadable model list.",
        ) from exc

    if not isinstance(response_data, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq returned an invalid model list.",
        )
    models_data = response_data.get("data")
    if not isinstance(models_data, list):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq returned an invalid model list.",
        )

    models: set[str] = set()
    for model_data in models_data:
        if not isinstance(model_data, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Groq returned an invalid model list.",
            )
        model_id = model_data.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Groq returned an invalid model list.",
            )
        normalized_id = model_id.strip()
        if normalized_id in STRUCTURED_OUTPUT_MODEL_IDS:
            models.add(normalized_id)
    return sorted(models)


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


def _response_header_int(response: httpx.Response, name: str) -> int | None:
    value = response.headers.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Groq returned an invalid {name} header.") from exc


def _provider_error_message(response: httpx.Response, api_key: str) -> str:
    default_message = (
        f"Groq request failed (HTTP {response.status_code}). Check the "
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
        f"Groq request failed (HTTP {response.status_code}): "
        f"{safe_message[:500]}"
    )
