from fastapi import HTTPException, status
from jinja2 import meta, nodes
from jinja2.sandbox import SandboxedEnvironment
from pydantic import ValidationError

from app.schemas.email_templates import EmailTemplateDraftResponse
from app.services.email_templates import (
    TEMPLATE_VARIABLES,
    validate_email_template_syntax,
)


def build_email_template_system_prompt() -> str:
    available_variables = "\n".join(
        f"- {{{{ {variable['path']} }}}}: {variable['description']}"
        for variable in TEMPLATE_VARIABLES
    )
    return (
        "Create a reusable B2B email template. Return only one JSON object "
        "with exactly these string fields: name, subject, and body_content. "
        "Do not add other fields, markdown, or commentary. The body_content "
        "must be email-safe HTML with no scripts, forms, or event handlers. "
        "Do not invent personal or company facts; use only the available "
        "template placeholders below. Keep the message concise and specific "
        "to the user's request.\n\n"
        "Available placeholders:\n"
        f"{available_variables}"
    )


def parse_email_template_draft(
    generated_text: str,
    provider_name: str,
) -> EmailTemplateDraftResponse:
    try:
        draft = EmailTemplateDraftResponse.model_validate_json(generated_text)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{provider_name} returned a draft in an unsupported format.",
        ) from exc

    try:
        validate_email_template_syntax(draft.subject, draft.body_content)
        validate_template_variables(draft.subject, draft.body_content)
    except HTTPException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"{provider_name} returned invalid template syntax or "
                "placeholders. Try generating the draft again."
            ),
        ) from exc

    return draft


def validate_template_variables(subject: str, body_content: str) -> None:
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
