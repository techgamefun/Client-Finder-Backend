from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AI Lead Generation API"
    app_version: str = "0.1.0"
    environment: str = "development"

    database_url: str

    tavily_api_key: str | None = None
    octoparse_api_key: str | None = None
    settings_encryption_key: str | None = None
    google_pagespeed_api_key: str | None = None
    crawler_service_url: str = Field(
        default="http://localhost:8001",
        validation_alias="WEB_CRAWLER_SERVICE_URL",
    )
    upstash_redis_rest_url: str | None = None
    upstash_redis_rest_token: str | None = None
    frontend_origins: str = Field(validation_alias="FRONTEND_URL")
    run_worker_in_api: bool = Field(
        default=False,
        validation_alias="RUN_WORKER_IN_API",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
