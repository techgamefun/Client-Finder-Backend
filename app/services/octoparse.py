import json
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models import DiscoveryProviderJob
from app.schemas.discovery import (
    DiscoveryProvider,
    DiscoveryProviderJobResponse,
    DiscoveryResult,
)
from app.schemas.settings import (
    OctoparseTemplateOption,
    OctoparseTemplateSearchResponse,
)
from app.services.application_settings import (
    get_octoparse_api_key,
    get_application_settings,
)


OCTOPARSE_API_BASE_URL = "https://openapi.octoparse.com"
OCTOPARSE_EXTERNAL_USER_ID = "business-intelligence-agent"
OCTOPARSE_REQUEST_TIMEOUT = 30.0


async def search_octoparse_templates(
    keyword: str,
    db: Session,
) -> OctoparseTemplateSearchResponse:
    api_key = _get_api_key(db)
    try:
        async with httpx.AsyncClient(timeout=OCTOPARSE_REQUEST_TIMEOUT) as client:
            response = await client.get(
                f"{OCTOPARSE_API_BASE_URL}/api/agentTools/searchTemplates",
                params={"keyword": keyword, "page": 1, "limit": 20},
                headers={"x-api-key": api_key},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Octoparse template search timed out.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Octoparse to search templates.",
        ) from exc

    data = _response_data(response, "template search")
    raw_templates = data.get("templates")
    if not isinstance(raw_templates, list):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Octoparse returned an invalid template list.",
        )

    templates: list[OctoparseTemplateOption] = []
    for template in raw_templates:
        if not isinstance(template, dict):
            continue
        template_name = (
            template.get("templateName")
            or template.get("name")
            or template.get("recommendedTemplateName")
        )
        if not isinstance(template_name, str) or not template_name.strip():
            continue
        templates.append(
            OctoparseTemplateOption(
                template_name=template_name,
                slug=_optional_string(template.get("slug")),
                execution_mode=_optional_string(
                    template.get("executionMode"),
                ),
                description=_optional_string(template.get("description")),
                input_schema=_list_of_dicts(template.get("inputSchema")),
                output_schema=_list_of_dicts(template.get("outputSchema")),
                source_tree=template.get("sourceTree"),
                source_summary=template.get("sourceSummary"),
            )
        )

    return OctoparseTemplateSearchResponse(
        recommended_template_name=_optional_string(
            data.get("recommendedTemplateName"),
        ),
        templates=templates,
    )


async def start_octoparse_search(
    query: str,
    max_results: int,
    db: Session,
) -> tuple[str, str | None, dict[str, str | None]]:
    api_key = _get_api_key(db)
    app_settings = get_application_settings(db)
    template = app_settings.octoparse_template
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Select and configure an Octoparse template in Settings first.",
        )

    parameters = dict(template.additional_parameters)
    parameters[template.query_field] = query
    payload = {
        "templateName": template.template_name,
        "taskName": f"Lead discovery: {query[:180]}",
        "parameters": json.dumps(parameters, ensure_ascii=False),
        "maxCollectedRows": max_results,
    }

    try:
        async with httpx.AsyncClient(timeout=OCTOPARSE_REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{OCTOPARSE_API_BASE_URL}/api/agentTools/executeTask",
                json=payload,
                headers={
                    "x-api-key": api_key,
                    "x-external-user-id": OCTOPARSE_EXTERNAL_USER_ID,
                },
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Octoparse did not respond while starting the search task.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Octoparse to start the search task.",
        ) from exc

    data = _response_data(response, "task start")
    task_id = data.get("taskId")
    task_status = data.get("status")
    if (
        not data.get("success", True)
        or task_status
        in {"invalid", "start_failed", "failed", "awaiting_source_selection"}
        or not isinstance(task_id, str)
        or not task_id
    ):
        guidance = data.get("recoverySuggestion")
        detail = "Octoparse could not start the configured template."
        if isinstance(guidance, str) and guidance:
            detail = f"{detail} {guidance}"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        )
    lot_no = data.get("lotNo")
    result_mapping = {
        "title_field": template.title_field,
        "url_field": template.url_field,
        "content_field": template.content_field,
        "score_field": template.score_field,
    }
    return (
        task_id,
        lot_no if isinstance(lot_no, str) and lot_no else None,
        result_mapping,
    )


async def poll_octoparse_job(
    job: DiscoveryProviderJob,
    db: Session,
) -> DiscoveryProviderJobResponse:
    if job.status in {"succeeded", "failed"}:
        return _serialize_job(job)

    try:
        api_key = _get_api_key(db)
    except HTTPException as exc:
        job.status = "failed"
        job.error = str(exc.detail)
        db.commit()
        db.refresh(job)
        return _serialize_job(job)
    params: dict[str, str | int] = {
        "taskId": job.external_task_id,
        "exportFileType": "JSON",
        "previewRows": job.max_results,
    }
    if job.lot_no:
        params["lotNo"] = job.lot_no
    url = f"{OCTOPARSE_API_BASE_URL}/api/agentTools/exportData?{urlencode(params)}"

    try:
        async with httpx.AsyncClient(timeout=OCTOPARSE_REQUEST_TIMEOUT) as client:
            response = await client.get(
                url,
                headers={"x-api-key": api_key},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Octoparse export-status request timed out.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Octoparse to check the search task.",
        ) from exc

    if response.status_code in {401, 403}:
        job.status = "failed"
        job.error = "Octoparse rejected the configured API key."
        db.commit()
        db.refresh(job)
        return _serialize_job(job)

    data = _response_data(response, "export status")
    task_status = data.get("status")
    if task_status in {"collecting", "exporting", "accepted", "ready"}:
        job.status = "running"
    elif task_status == "no_data":
        job.status = "succeeded"
        job.results = []
    elif task_status == "exported":
        try:
            if (
                data.get("sampleData") == []
                and data.get("dataTotal", 0) > 0
            ):
                raise ValueError(
                    "Octoparse exported rows without a sample preview.",
                )
            results, warning = _map_sample_data(
                data.get("sampleData"),
                job.max_results,
                job.result_mapping,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            job.status = "failed"
            job.error = str(exc)
        else:
            job.status = "succeeded"
            job.results = [
                result.model_dump(mode="json") for result in results
            ]
            job.warning = warning
    elif task_status in {"failed", "invalid"}:
        job.status = "failed"
        error = data.get("error") or data.get("recoverySuggestion")
        job.error = (
            error[:1000]
            if isinstance(error, str) and error
            else "Octoparse reported that the search task failed."
        )
    else:
        job.status = "failed"
        job.error = "Octoparse returned an unrecognized export status."

    db.commit()
    db.refresh(job)
    return _serialize_job(job)


def _get_api_key(db: Session) -> str:
    api_key = get_octoparse_api_key(db)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Octoparse is not configured. Add OCTOPARSE_API_KEY or save a key in Settings.",
        )
    return api_key


def _response_data(response: httpx.Response, operation: str) -> dict[str, Any]:
    if response.status_code in {401, 403}:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Octoparse rejected the configured API key.",
        )
    if response.status_code == 429:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Octoparse rate-limited the request. Try again later.",
        )
    if response.is_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Octoparse {operation} failed with HTTP {response.status_code}.",
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Octoparse returned invalid JSON for {operation}.",
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Octoparse returned an invalid {operation} response.",
        )
    data = body.get("data", body)
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Octoparse returned an invalid {operation} response.",
        )
    return data


def _map_sample_data(
    sample_data: Any,
    max_results: int,
    mapping: dict[str, str | None],
) -> tuple[list[DiscoveryResult], str | None]:
    if not isinstance(sample_data, list):
        raise ValueError(
            "Octoparse finished without sample data. Check the template output fields.",
        )
    title_field = mapping.get("title_field")
    url_field = mapping.get("url_field")
    content_field = mapping.get("content_field")
    score_field = mapping.get("score_field")
    if not title_field or not url_field:
        raise ValueError(
            "The saved Octoparse result mapping is missing its title or URL field.",
        )
    results: list[DiscoveryResult] = []
    dropped_rows = 0
    for row in sample_data[:max_results]:
        if not isinstance(row, dict):
            dropped_rows += 1
            continue
        title = _field_text(row, title_field)
        url = _field_text(row, url_field)
        if not title or not url:
            dropped_rows += 1
            continue
        content = (
            _field_text(row, content_field)
            if content_field
            else json.dumps(row, ensure_ascii=False, default=str)
        )
        score = 0.5
        if score_field:
            score_value = row.get(score_field)
            if score_value is not None:
                try:
                    score = float(score_value)
                except (TypeError, ValueError):
                    dropped_rows += 1
                    continue
                score = min(max(score, 0.0), 1.0)
        try:
            results.append(
                DiscoveryResult(
                    title=title,
                    url=url,
                    content=content[:5000],
                    score=score,
                    provider=DiscoveryProvider.OCTOPARSE,
                )
            )
        except ValidationError:
            dropped_rows += 1

    if not results and sample_data:
        raise ValueError(
            "Octoparse returned rows but none matched the configured title and URL fields.",
        )
    warning = (
        f"Skipped {dropped_rows} Octoparse rows that did not match the saved field mapping."
        if dropped_rows
        else None
    )
    return results, warning


def _field_text(row: dict[str, Any], field: str | None) -> str:
    if field is None:
        return ""
    value = row.get(field)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value).strip()


def _serialize_job(job: DiscoveryProviderJob) -> DiscoveryProviderJobResponse:
    return DiscoveryProviderJobResponse(
        id=job.id,
        provider=DiscoveryProvider.OCTOPARSE,
        status=job.status,
        results=job.results or [],
        error=job.error,
        warning=job.warning,
    )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
