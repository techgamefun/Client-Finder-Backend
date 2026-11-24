import re
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_TOKEN_EXPIRY_PATTERN = re.compile(r"^([1-9][0-9]*)(s|m|h|d)$")
_TOKEN_EXPIRY_MULTIPLIERS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
}
_ACCESS_TOKEN_MAX_SECONDS = 24 * 60 * 60
_REFRESH_TOKEN_MAX_SECONDS = 365 * 24 * 60 * 60


def parse_token_expiry(value: str, *, maximum_seconds: int) -> int:
    match = _TOKEN_EXPIRY_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError("Token expiry must use a positive s, m, h, or d duration.")
    amount = int(match.group(1))
    seconds = amount * _TOKEN_EXPIRY_MULTIPLIERS[match.group(2)]
    if seconds > maximum_seconds:
        raise ValueError("Token expiry exceeds the allowed maximum.")
    return seconds


class Settings(BaseSettings):
    app_name: str = "AI Lead Generation API"
    app_version: str = "0.1.0"
    environment: str = "development"

    database_url: str

    tavily_api_key: str | None = None
    octoparse_api_key: str | None = None
    access_token_secret: str | None = None
    access_token_expiry: str = "15m"
    refresh_token_secret: str | None = None
    refresh_token_expiry: str = "168h"
    google_pagespeed_api_key: str | None = None
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = None
    google_oauth_state_secret: str | None = None
    email_unsubscribe_secret: str | None = None
    email_monitor_interval_seconds: int = Field(
        default=300,
        ge=30,
        le=86_400,
        validation_alias="EMAIL_MONITOR_INTERVAL_SECONDS",
    )
    gmail_token_encryption_key: str | None = None
    crawler_service_url: str = Field(
        default="http://localhost:8001",
        validation_alias="WEB_CRAWLER_SERVICE_URL",
    )
    crawler_service_urls: str | None = Field(
        default=None,
        validation_alias="WEB_CRAWLER_SERVICE_URLS",
    )
    crawl_job_concurrency: int = Field(
        default=5,
        ge=1,
        le=10,
        validation_alias="CRAWL_JOB_CONCURRENCY",
    )
    crawler_service_shared_token: str | None = Field(
        default=None,
        validation_alias="CRAWLER_SERVICE_SHARED_TOKEN",
    )
    python_worker_website_jobs_enabled: bool = Field(
        default=True,
        validation_alias="PYTHON_WORKER_WEBSITE_JOBS_ENABLED",
    )
    python_worker_campaign_sending_enabled: bool = Field(
        default=True,
        validation_alias="PYTHON_WORKER_CAMPAIGN_SENDING_ENABLED",
    )
    python_worker_gmail_monitoring_enabled: bool = Field(
        default=True,
        validation_alias="PYTHON_WORKER_GMAIL_MONITORING_ENABLED",
    )
    upstash_redis_rest_url: str | None = None
    upstash_redis_rest_token: str | None = None
    cloudinary_cloud_name: str | None = None
    cloudinary_api_key: str | None = None
    cloudinary_api_secret: str | None = None
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_subject: str | None = None
    frontend_origins: str = Field(validation_alias="FRONTEND_URL")
    run_worker_in_api: bool = Field(
        default=False,
        validation_alias="RUN_WORKER_IN_API",
    )

    @field_validator("access_token_expiry")
    @classmethod
    def validate_access_token_expiry(cls, value: str) -> str:
        parse_token_expiry(value, maximum_seconds=_ACCESS_TOKEN_MAX_SECONDS)
        return value

    @field_validator("refresh_token_expiry")
    @classmethod
    def validate_refresh_token_expiry(cls, value: str) -> str:
        parse_token_expiry(value, maximum_seconds=_REFRESH_TOKEN_MAX_SECONDS)
        return value

    @field_validator("crawler_service_urls")
    @classmethod
    def validate_crawler_service_urls(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        urls = [url.strip().rstrip("/") for url in value.split(",")]
        if not urls or any(not url for url in urls):
            raise ValueError(
                "WEB_CRAWLER_SERVICE_URLS must be a comma-separated URL list.",
            )
        for url in urls:
            if url.casefold().endswith("/health"):
                url = url[:-len("/health")].rstrip("/")
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError(
                    "Each WEB_CRAWLER_SERVICE_URLS entry must be an HTTP(S) URL.",
                )
        return value

    @field_validator("vapid_subject")
    @classmethod
    def validate_vapid_subject(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized and not (
            normalized.startswith("mailto:") or normalized.startswith("https://")
        ):
            raise ValueError("VAPID_SUBJECT must be a mailto: or https:// URI.")
        return normalized or None

    @model_validator(mode="after")
    def validate_token_secrets(self) -> "Settings":
        access_secret = self.access_token_secret
        refresh_secret = self.refresh_token_secret
        if not access_secret or not refresh_secret:
            raise ValueError(
                "ACCESS_TOKEN_SECRET and REFRESH_TOKEN_SECRET are required."
            )
        for name, value in (
            ("ACCESS_TOKEN_SECRET", access_secret),
            ("REFRESH_TOKEN_SECRET", refresh_secret),
        ):
            if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){32,}", value):
                raise ValueError(
                    f"{name} must be at least 64 hexadecimal characters."
                )
        if access_secret.casefold() == refresh_secret.casefold():
            raise ValueError(
                "ACCESS_TOKEN_SECRET and REFRESH_TOKEN_SECRET must be different."
            )
        if (
            self.email_unsubscribe_secret
            and len(self.email_unsubscribe_secret) < 32
        ):
            raise ValueError(
                "EMAIL_UNSUBSCRIBE_SECRET must contain at least 32 characters."
            )
        return self

    @property
    def web_push_configured(self) -> bool:
        return bool(
            self.vapid_public_key
            and self.vapid_private_key
            and self.vapid_subject
        )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
