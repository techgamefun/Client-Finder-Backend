from dataclasses import dataclass
from hmac import compare_digest

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ApplicationSettings
from app.schemas.settings import (
    ApplicationSettingsResponse,
    ApplicationSettingsUpdate,
)


DEFAULT_CRAWL_MAX_PAGES = 6
DEFAULT_CRAWL_MAX_DEPTH = 2
SETTINGS_ROW_ID = 1


@dataclass(frozen=True)
class CrawlSettings:
    max_pages: int
    max_depth: int


def authorize_settings_admin(authorization: str | None) -> None:
    expected_token = settings.settings_admin_token
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Settings administration is not configured.",
        )

    scheme, separator, supplied_token = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not compare_digest(supplied_token, expected_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid settings administrator token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_crawl_settings(db: Session) -> CrawlSettings:
    row = db.get(ApplicationSettings, SETTINGS_ROW_ID)
    if row is None:
        return CrawlSettings(
            max_pages=DEFAULT_CRAWL_MAX_PAGES,
            max_depth=DEFAULT_CRAWL_MAX_DEPTH,
        )
    return CrawlSettings(
        max_pages=row.crawl_max_pages,
        max_depth=row.crawl_max_depth,
    )


def get_tavily_api_key(db: Session) -> str | None:
    row = db.get(ApplicationSettings, SETTINGS_ROW_ID)
    if row and row.tavily_api_key_encrypted:
        return _decrypt_tavily_key(row.tavily_api_key_encrypted)
    return settings.tavily_api_key


def get_application_settings(db: Session) -> ApplicationSettingsResponse:
    row = db.get(ApplicationSettings, SETTINGS_ROW_ID)
    is_database_key_configured = bool(
        row and row.tavily_api_key_encrypted
    )
    is_environment_key_configured = bool(settings.tavily_api_key)
    crawl_settings = get_crawl_settings(db)
    return ApplicationSettingsResponse(
        crawl_max_pages=crawl_settings.max_pages,
        crawl_max_depth=crawl_settings.max_depth,
        tavily_configured=(
            is_database_key_configured or is_environment_key_configured
        ),
        tavily_key_source=(
            "database"
            if is_database_key_configured
            else "environment"
            if is_environment_key_configured
            else "unset"
        ),
    )


def save_application_settings(
    payload: ApplicationSettingsUpdate,
    db: Session,
) -> ApplicationSettingsResponse:
    row = db.get(ApplicationSettings, SETTINGS_ROW_ID)
    if row is None:
        row = ApplicationSettings(id=SETTINGS_ROW_ID)
        db.add(row)

    row.crawl_max_pages = payload.crawl_max_pages
    row.crawl_max_depth = payload.crawl_max_depth
    if payload.tavily_api_key is not None:
        key = payload.tavily_api_key.strip()
        row.tavily_api_key_encrypted = (
            _encrypt_tavily_key(key) if key else None
        )

    try:
        db.commit()
        db.refresh(row)
    except SQLAlchemyError:
        db.rollback()
        raise

    return get_application_settings(db)


def _cipher() -> Fernet:
    key = settings.settings_encryption_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tavily key encryption is not configured on the server.",
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The server's Tavily key encryption configuration is invalid.",
        ) from exc


def _encrypt_tavily_key(key: str) -> str:
    return _cipher().encrypt(key.encode("utf-8")).decode("ascii")


def _decrypt_tavily_key(encrypted_key: str) -> str:
    try:
        return _cipher().decrypt(encrypted_key.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The saved Tavily key cannot be decrypted. Verify the server encryption key.",
        ) from exc
