from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AI Lead Generation API"
    app_version: str = "0.1.0"
    environment: str = "development"

    database_url: str

    tavily_api_key: str | None = None
    google_pagespeed_api_key: str | None = None
    frontend_origins: str = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3002"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
