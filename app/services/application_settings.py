from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ApplicationSettings,
    UserProviderCredential,
)
from app.schemas.discovery import DiscoveryProvider
from app.schemas.settings import (
    ApplicationSettingsResponse,
    ApplicationSettingsUpdate,
)
from app.tenant_context import get_tenant_context


DEFAULT_CRAWL_MAX_PAGES = 6
DEFAULT_CRAWL_MAX_DEPTH = 2
@dataclass(frozen=True)
class CrawlSettings:
    max_pages: int
    max_depth: int


@dataclass(frozen=True)
class HypeStatSettings:
    api_key: str | None
    auto_fetch_enabled: bool


def get_crawl_settings(db: Session) -> CrawlSettings:
    row = _get_settings_row(db)
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
    return get_user_provider_api_key(db, "tavily")


def get_octoparse_api_key(db: Session) -> str | None:
    return get_user_provider_api_key(db, "octoparse")


def get_user_provider_api_key(
    db: Session,
    provider: str,
    *,
    user_id=None,
) -> str | None:
    if user_id is None:
        context = get_tenant_context() or db.info.get("tenant_context")
        if context is None:
            return None
        user_id = context.user_id
    credential = db.scalar(
        select(UserProviderCredential).where(
            UserProviderCredential.user_id == user_id,
            UserProviderCredential.provider == provider,
        )
    )
    return credential.api_key if credential is not None else None


def save_user_provider_api_key(
    db: Session,
    provider: str,
    key: str | None,
    *,
    user_id=None,
) -> None:
    if user_id is None:
        context = get_tenant_context() or db.info.get("tenant_context")
        if context is None:
            raise RuntimeError("Authenticated user context is required.")
        user_id = context.user_id
    credential = db.scalar(
        select(UserProviderCredential).where(
            UserProviderCredential.user_id == user_id,
            UserProviderCredential.provider == provider,
        )
    )
    normalized_key = key.strip() if key else ""
    if not normalized_key:
        if credential is not None:
            db.delete(credential)
        return
    if credential is None:
        db.add(
            UserProviderCredential(
                user_id=user_id,
                provider=provider,
                api_key=normalized_key,
            )
        )
    else:
        credential.api_key = normalized_key


def get_hypestat_settings(db: Session) -> HypeStatSettings:
    row = _get_settings_row(db)
    return HypeStatSettings(
        api_key=get_user_provider_api_key(db, "hypestat"),
        auto_fetch_enabled=(
            row.hypestat_auto_fetch_enabled if row is not None else True
        ),
    )


def get_selected_discovery_tools(db: Session) -> list[DiscoveryProvider]:
    row = _get_settings_row(db)
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
    row = _get_settings_row(db)
    tavily_configured = bool(get_user_provider_api_key(db, "tavily"))
    octoparse_configured = bool(get_user_provider_api_key(db, "octoparse"))
    crawl_settings = get_crawl_settings(db)
    return ApplicationSettingsResponse(
        crawl_max_pages=crawl_settings.max_pages,
        crawl_max_depth=crawl_settings.max_depth,
        tavily_configured=tavily_configured,
        tavily_key_source="database" if tavily_configured else "unset",
        octoparse_configured=octoparse_configured,
        octoparse_key_source="database" if octoparse_configured else "unset",
        hypestat_configured=bool(get_user_provider_api_key(db, "hypestat")),
        pagespeed_configured=bool(get_user_provider_api_key(db, "pagespeed")),
        pagespeed_key_source=(
            "database"
            if get_user_provider_api_key(db, "pagespeed")
            else "unset"
        ),
        hypestat_auto_fetch_enabled=(
            row.hypestat_auto_fetch_enabled if row is not None else True
        ),
        hypestat_service_auth_configured=bool(
            settings.crawler_service_shared_token
        ),
        selected_discovery_tools=get_selected_discovery_tools(db),
        tavily_default_search_depth=(
            row.tavily_default_search_depth if row else "basic"
        ),
        tavily_default_country=(
            row.tavily_default_country if row else None
        ),
        tavily_default_exclude_domains=(
            row.tavily_default_exclude_domains if row else []
        ),
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
    row = _get_settings_row(db)
    if row is None:
        row = ApplicationSettings()
        db.add(row)

    if payload.crawl_max_pages is not None:
        row.crawl_max_pages = payload.crawl_max_pages
    if payload.crawl_max_depth is not None:
        row.crawl_max_depth = payload.crawl_max_depth
    if payload.tavily_api_key is not None:
        save_user_provider_api_key(db, "tavily", payload.tavily_api_key)
    if payload.octoparse_api_key is not None:
        save_user_provider_api_key(db, "octoparse", payload.octoparse_api_key)
    if payload.hypestat_api_key is not None:
        save_user_provider_api_key(db, "hypestat", payload.hypestat_api_key)
    if payload.pagespeed_api_key is not None:
        save_user_provider_api_key(db, "pagespeed", payload.pagespeed_api_key)
    if payload.hypestat_auto_fetch_enabled is not None:
        row.hypestat_auto_fetch_enabled = payload.hypestat_auto_fetch_enabled
    if payload.tavily_default_search_depth is not None:
        row.tavily_default_search_depth = payload.tavily_default_search_depth
    if "tavily_default_country" in payload.model_fields_set:
        row.tavily_default_country = payload.tavily_default_country
    if "tavily_default_exclude_domains" in payload.model_fields_set:
        row.tavily_default_exclude_domains = (
            payload.tavily_default_exclude_domains or []
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


def _get_settings_row(db: Session) -> ApplicationSettings | None:
    context = get_tenant_context() or db.info.get("tenant_context")
    if context is None:
        return None
    return db.scalar(
        select(ApplicationSettings).where(
            ApplicationSettings.organization_id == context.organization_id
        )
    )


