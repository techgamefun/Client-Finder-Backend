import re

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
    crawler_service_shared_token: str | None = Field(
        default=None,
        validation_alias="CRAWLER_SERVICE_SHARED_TOKEN",
    )
    upstash_redis_rest_url: str | None = None
    upstash_redis_rest_token: str | None = None
    cloudinary_cloud_name: str | None = None
    cloudinary_api_key: str | None = None
    cloudinary_api_secret: str | None = None
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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
