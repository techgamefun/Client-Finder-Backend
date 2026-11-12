from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ApplicationSettings
from app.schemas.discovery import DiscoveryProvider
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
        return _decrypt_api_key(row.tavily_api_key_encrypted)
    return settings.tavily_api_key


def get_octoparse_api_key(db: Session) -> str | None:
    row = db.get(ApplicationSettings, SETTINGS_ROW_ID)
    if row and row.octoparse_api_key_encrypted:
        return _decrypt_api_key(row.octoparse_api_key_encrypted)
    return settings.octoparse_api_key


def get_selected_discovery_tools(db: Session) -> list[DiscoveryProvider]:
    row = db.get(ApplicationSettings, SETTINGS_ROW_ID)
    if row is None or row.selected_discovery_tools is None:
        return [DiscoveryProvider.TAVILY]
    try:
        return [DiscoveryProvider(tool) for tool in row.selected_discovery_tools]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The saved discovery-tool selection is invalid.",
        ) from exc


def get_application_settings(db: Session) -> ApplicationSettingsResponse:
    row = db.get(ApplicationSettings, SETTINGS_ROW_ID)
    tavily_database_key_configured = bool(
        row and row.tavily_api_key_encrypted
    )
    tavily_environment_key_configured = bool(settings.tavily_api_key)
    octoparse_database_key_configured = bool(
        row and row.octoparse_api_key_encrypted
    )
    octoparse_environment_key_configured = bool(settings.octoparse_api_key)
    crawl_settings = get_crawl_settings(db)
    return ApplicationSettingsResponse(
        crawl_max_pages=crawl_settings.max_pages,
        crawl_max_depth=crawl_settings.max_depth,
        tavily_configured=(
            tavily_database_key_configured or tavily_environment_key_configured
        ),
        tavily_key_source=(
            "database"
            if tavily_database_key_configured
            else "environment"
            if tavily_environment_key_configured
            else "unset"
        ),
        octoparse_configured=(
            octoparse_database_key_configured
            or octoparse_environment_key_configured
        ),
        octoparse_key_source=(
            "database"
            if octoparse_database_key_configured
            else "environment"
            if octoparse_environment_key_configured
            else "unset"
        ),
        selected_discovery_tools=get_selected_discovery_tools(db),
        octoparse_template=(
            row.octoparse_template
            if row and row.octoparse_template
            else None
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

    if payload.crawl_max_pages is not None:
        row.crawl_max_pages = payload.crawl_max_pages
    if payload.crawl_max_depth is not None:
        row.crawl_max_depth = payload.crawl_max_depth
    if payload.tavily_api_key is not None:
        key = payload.tavily_api_key.strip()
        row.tavily_api_key_encrypted = (
            _encrypt_api_key(key) if key else None
        )
    if payload.octoparse_api_key is not None:
        key = payload.octoparse_api_key.strip()
        row.octoparse_api_key_encrypted = (
            _encrypt_api_key(key) if key else None
        )
    if payload.selected_discovery_tools is not None:
        row.selected_discovery_tools = [
            provider.value for provider in payload.selected_discovery_tools
        ]
    if "octoparse_template" in payload.model_fields_set:
        row.octoparse_template = (
            payload.octoparse_template.model_dump(mode="json")
            if payload.octoparse_template is not None
            else None
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
            detail="Provider API-key encryption is not configured on the server.",
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The server's provider API-key encryption configuration is invalid.",
        ) from exc


def _encrypt_api_key(key: str) -> str:
    return _cipher().encrypt(key.encode("utf-8")).decode("ascii")


def _decrypt_api_key(encrypted_key: str) -> str:
    try:
        return _cipher().decrypt(encrypted_key.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A saved provider API key cannot be decrypted. Verify the server encryption key.",
        ) from exc
