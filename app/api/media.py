import logging
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy import update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    get_db,
    require_admin_context,
    require_authenticated_context,
)
from app.config import settings
from app.http_clients import async_http_client
from app.models import Organization, User
from app.schemas.media import ImageAssetResponse
from app.tenant_context import TenantContext


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/media", tags=["Media"])

DatabaseSession = Annotated[Session, Depends(get_db)]
CurrentContext = Annotated[
    TenantContext,
    Depends(require_authenticated_context),
]
AdminContext = Annotated[
    TenantContext,
    Depends(require_admin_context),
]
ImageFile = Annotated[UploadFile, File()]

MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_SIGNATURES = {
    "image/jpeg": lambda content: content.startswith(b"\xff\xd8\xff"),
    "image/png": lambda content: content.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/webp": lambda content: (
        len(content) >= 12
        and content.startswith(b"RIFF")
        and content[8:12] == b"WEBP"
    ),
}


@router.post("/profile-photo", response_model=ImageAssetResponse)
async def upload_profile_photo(
    context: CurrentContext,
    db: DatabaseSession,
    file: ImageFile,
) -> ImageAssetResponse:
    await run_in_threadpool(_ensure_user_exists, db, context.user_id)
    content, content_type = await _read_image(file)
    image_url = await _upload_image(
        content,
        content_type,
        f"users/{context.user_id}/profile-photo",
    )
    await run_in_threadpool(
        _save_user_profile_photo,
        db,
        context.user_id,
        image_url,
    )
    return ImageAssetResponse(url=image_url)


@router.delete("/profile-photo", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile_photo(
    context: CurrentContext,
    db: DatabaseSession,
) -> None:
    await run_in_threadpool(_ensure_user_exists, db, context.user_id)
    await _delete_image(f"users/{context.user_id}/profile-photo")
    await run_in_threadpool(
        _save_user_profile_photo,
        db,
        context.user_id,
        None,
    )


@router.post(
    "/organization/{asset_name}",
    response_model=ImageAssetResponse,
)
async def upload_organization_image(
    asset_name: Literal["logo", "banner"],
    context: AdminContext,
    db: DatabaseSession,
    file: ImageFile,
) -> ImageAssetResponse:
    await run_in_threadpool(
        _ensure_organization_exists,
        db,
        context.organization_id,
    )
    content, content_type = await _read_image(file)
    image_url = await _upload_image(
        content,
        content_type,
        f"organizations/{context.organization_id}/{asset_name}",
    )
    await run_in_threadpool(
        _save_organization_image,
        db,
        context.organization_id,
        asset_name,
        image_url,
    )
    return ImageAssetResponse(url=image_url)


@router.delete(
    "/organization/{asset_name}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_organization_image(
    asset_name: Literal["logo", "banner"],
    context: AdminContext,
    db: DatabaseSession,
) -> None:
    await run_in_threadpool(
        _ensure_organization_exists,
        db,
        context.organization_id,
    )
    await _delete_image(
        f"organizations/{context.organization_id}/{asset_name}",
    )
    await run_in_threadpool(
        _save_organization_image,
        db,
        context.organization_id,
        asset_name,
        None,
    )


def _save_user_profile_photo(
    db: Session,
    user_id: UUID,
    image_url: str | None,
) -> None:
    result = db.execute(
        update(User)
        .where(User.id == user_id)
        .values(profile_photo_url=image_url)
    )
    if result.rowcount != 1:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your account is no longer available.",
        )
    db.commit()


def _save_organization_image(
    db: Session,
    organization_id: UUID,
    asset_name: Literal["logo", "banner"],
    image_url: str | None,
) -> None:
    values = (
        {"logo_url": image_url}
        if asset_name == "logo"
        else {"banner_url": image_url}
    )
    result = db.execute(
        update(Organization)
        .where(Organization.id == organization_id)
        .values(**values)
    )
    if result.rowcount != 1:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found.",
        )
    db.commit()


def _ensure_user_exists(db: Session, user_id: UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your account is no longer available.",
        )
    return user


def _ensure_organization_exists(
    db: Session,
    organization_id: UUID,
) -> Organization:
    organization = db.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found.",
        )
    return organization


async def _read_image(file: UploadFile) -> tuple[bytes, str]:
    content_type = (file.content_type or "").split(";", maxsplit=1)[0].lower()
    signature_check = IMAGE_SIGNATURES.get(content_type)
    if signature_check is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Use a JPEG, PNG, or WebP image.",
        )
    content = await file.read(MAX_IMAGE_BYTES + 1)
    if not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The uploaded image is empty.",
        )
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Images must be 8 MB or smaller.",
        )
    if not signature_check(content):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="The uploaded file does not match its image type.",
        )
    return content, content_type


def _cloudinary_credentials() -> tuple[str, str, str]:
    cloud_name = settings.cloudinary_cloud_name
    api_key = settings.cloudinary_api_key
    api_secret = settings.cloudinary_api_secret
    if not cloud_name or not api_key or not api_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloudinary image storage is not configured.",
        )
    return cloud_name, api_key, api_secret


async def _cloudinary_post(
    action: Literal["upload", "destroy"],
    parameters: dict[str, str],
    api_key: str,
    api_secret: str,
    *,
    file: tuple[str, bytes, str] | None = None,
) -> dict[str, object]:
    cloud_name = settings.cloudinary_cloud_name
    if not cloud_name:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloudinary image storage is not configured.",
        )
    url = (
        f"https://api.cloudinary.com/v1_1/"
        f"{quote(cloud_name, safe='')}/image/{action}"
    )
    try:
        async with async_http_client(timeout=45) as client:
            response = await client.post(
                url,
                data=parameters,
                files={"file": file} if file is not None else None,
                auth=(api_key, api_secret),
            )
    except httpx.TimeoutException as exc:
        logger.warning("Cloudinary %s request timed out.", action)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Cloudinary did not respond in time.",
        ) from exc
    except httpx.RequestError as exc:
        logger.warning("Cloudinary %s request failed: %s", action, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not connect to Cloudinary.",
        ) from exc

    try:
        result = response.json()
    except ValueError as exc:
        logger.warning(
            "Cloudinary %s returned invalid JSON with status %s.",
            action,
            response.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Cloudinary returned an invalid response.",
        ) from exc
    if not isinstance(result, dict):
        logger.warning("Cloudinary %s returned an unexpected response.", action)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Cloudinary returned an invalid response.",
        )
    if not response.is_success:
        logger.warning(
            "Cloudinary %s failed with status %s.",
            action,
            response.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Cloudinary could not process the image request.",
        )
    return result


async def _upload_image(
    content: bytes,
    content_type: str,
    public_id: str,
) -> str:
    cloud_name, api_key, api_secret = _cloudinary_credentials()
    result = await _cloudinary_post(
        "upload",
        {
            "overwrite": "true",
            "public_id": public_id,
        },
        api_key,
        api_secret,
        file=("image", content, content_type),
    )
    secure_url = result.get("secure_url")
    if not isinstance(secure_url, str):
        logger.warning("Cloudinary upload response did not contain secure_url.")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Cloudinary did not return a secure image URL.",
        )
    parsed_url = urlsplit(secure_url)
    expected_prefix = f"/{cloud_name}/image/upload/"
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "res.cloudinary.com"
        or not parsed_url.path.startswith(expected_prefix)
    ):
        logger.warning("Cloudinary returned an unexpected image URL.")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Cloudinary returned an invalid image URL.",
        )
    return secure_url


async def _delete_image(public_id: str) -> None:
    _, api_key, api_secret = _cloudinary_credentials()
    result = await _cloudinary_post(
        "destroy",
        {"invalidate": "true", "public_id": public_id},
        api_key,
        api_secret,
    )
    if result.get("result") not in {"ok", "not found"}:
        logger.warning("Cloudinary did not delete the requested image.")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Cloudinary could not remove the image.",
        )
