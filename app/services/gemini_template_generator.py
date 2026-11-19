import json

import httpx
from fastapi import HTTPException, status
from jinja2 import meta, nodes
from jinja2.sandbox import SandboxedEnvironment
from pydantic import ValidationError

from app.schemas.email_templates import (
    EmailTemplateDraftResponse,
)
from app.services.email_templates import (
    TEMPLATE_VARIABLES,
    validate_email_template_syntax,
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
    available_variables = "\n".join(
        f"- {{{{ {variable['path']} }}}}: {variable['description']}"
        for variable in TEMPLATE_VARIABLES
    )
    request_payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "Create a reusable B2B email template. Return only "
                        "the requested JSON object. The body_content must be "
                        "email-safe HTML with no scripts, forms, or event "
                        "handlers. Do not invent personal or company facts; "
                        "use only the available template placeholders below. "
                        "Keep the message concise and specific to the user's "
                        "request.\n\nAvailable placeholders:\n"
                        f"{available_variables}"
                    ),
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
        async with httpx.AsyncClient(
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

    try:
        draft = EmailTemplateDraftResponse.model_validate_json(generated_text)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gemini returned a draft in an unsupported format.",
        ) from exc

    try:
        validate_email_template_syntax(draft.subject, draft.body_content)
        _validate_template_variables(draft.subject, draft.body_content)
    except HTTPException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Gemini returned invalid template syntax or placeholders. "
                "Try generating the draft again."
            ),
        ) from exc

    return draft


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


def _validate_template_variables(subject: str, body_content: str) -> None:
    allowed_paths = {variable["path"] for variable in TEMPLATE_VARIABLES}
    allowed_roots = {
        path.split(".", maxsplit=1)[0] for path in allowed_paths
    }
    environment = SandboxedEnvironment()
    parsed = environment.parse(f"{subject}\n{body_content}")
    undeclared = meta.find_undeclared_variables(parsed)
    unsupported = undeclared - allowed_roots
    if unsupported:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Generated template contains unsupported placeholders.",
        )
    attribute_roots: set[int] = set()
    for attribute in parsed.find_all(nodes.Getattr):
        path = _attribute_path(attribute)
        if path not in allowed_paths:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Generated template contains unsupported placeholders.",
            )
        root: nodes.Node = attribute
        while isinstance(root, nodes.Getattr):
            root = root.node
        if isinstance(root, nodes.Name):
            attribute_roots.add(id(root))
    for name in parsed.find_all(nodes.Name):
        if (
            name.ctx == "load"
            and name.name in allowed_roots
            and id(name) not in attribute_roots
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Generated template contains unsupported placeholders.",
            )
    if next(parsed.find_all(nodes.Getitem), None) is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Generated template contains unsupported placeholders.",
        )


def _attribute_path(attribute: nodes.Getattr) -> str | None:
    attributes: list[str] = []
    current: nodes.Node = attribute
    while isinstance(current, nodes.Getattr):
        attributes.append(current.attr)
        current = current.node
    if not isinstance(current, nodes.Name):
        return None
    return ".".join((current.name, *reversed(attributes)))
