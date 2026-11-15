
import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import SessionLocal, database_url
from app.auth_middleware import authenticate_request
from app.services.auth import validate_auth_configuration

from app.api.auth import router as auth_router
from app.api.companies import router as companies_router
from app.api.discovery import router as discovery_router
from app.api.gmail import router as gmail_router
from app.api.email_templates import router as email_templates_router
from app.api.pagespeed import router as pagespeed_router
from app.api.queue_events import router as queue_events_router
from app.api.settings import router as settings_router
from app.api.google_oauth import router as google_oauth_router
from app.api.websites import router as websites_router
from app.api.website_crawler import router as website_crawler_router

try:
    from alembic import command
    from alembic.config import Config
except ModuleNotFoundError:  # pragma: no cover - installed via requirements.txt
    command = None
    Config = None


logger = logging.getLogger(__name__)


def should_auto_migrate() -> bool:
    return not database_url.startswith("sqlite")


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_auth_configuration()
    if should_auto_migrate() and command is not None and Config is not None:
        alembic_cfg = Config("alembic.ini")
        logger.info("Starting database migrations.")
        command.upgrade(alembic_cfg, "head")
        logger.info("Database migrations completed.")

    worker_task: asyncio.Task[None] | None = None
    if settings.run_worker_in_api:
        from app.worker import run_worker

        logger.info("Starting the background worker inside the API process.")
        worker_task = asyncio.create_task(
            run_worker(),
            name="website-crawl-and-pagespeed-worker",
        )

    try:
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

frontend_origins = [
    origin.strip()
    for origin in settings.frontend_origins.split(",")
    if origin.strip()
]
app.state.frontend_origins = frontend_origins
app.state.session_factory = SessionLocal

app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.middleware("http")(authenticate_request)

app.include_router(
    auth_router,
    prefix="/api/v1",
)

app.include_router(
    google_oauth_router,
    prefix="/api/v1",
)

app.include_router(
    gmail_router,
    prefix="/api/v1",
)

app.include_router(
    email_templates_router,
    prefix="/api/v1",
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
    websites_router,
    prefix="/api/v1",
)

app.include_router(
    pagespeed_router,
    prefix="/api/v1",
)

app.include_router(
    queue_events_router,
    prefix="/api/v1",
)

app.include_router(
    settings_router,
    prefix="/api/v1",
)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {
        "status": "ok",
        "environment": settings.environment,
    }
