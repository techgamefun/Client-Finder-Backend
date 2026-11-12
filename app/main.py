
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import database_url

from app.api.companies import router as companies_router
from app.api.discovery import router as discovery_router
from app.api.pagespeed import router as pagespeed_router
from app.api.website_crawler import router as website_crawler_router

try:
    from alembic import command
    from alembic.config import Config
except ModuleNotFoundError:  # pragma: no cover - installed via requirements.txt
    command = None
    Config = None


def should_auto_migrate() -> bool:
    return not database_url.startswith("sqlite")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if should_auto_migrate() and command is not None and Config is not None:
        alembic_cfg = Config("alembic.ini")
        command.upgrade(alembic_cfg, "head")
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.frontend_origins.split(",")
        if origin.strip()
    ],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# Register the companies API.
app.include_router(
    companies_router,
    prefix="/api/v1",
)

# Register the lead discovery API separately.
app.include_router(
    discovery_router,
    prefix="/api/v1",
)

# Register the independent website crawler API.
app.include_router(
    website_crawler_router,
    prefix="/api/v1",
)

app.include_router(
    pagespeed_router,
    prefix="/api/v1",
)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {
        "status": "ok",
        "environment": settings.environment,
    }
