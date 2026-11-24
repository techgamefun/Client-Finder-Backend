
import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import SessionLocal, database_url
from app.http_clients import HTTPClientPool, install_http_client_pool, reset_http_client_pool
from app.auth_middleware import authenticate_request
from app.services.auth import validate_auth_configuration

from app.api.auth import router as auth_router
from app.api.media import router as media_router
from app.api.companies import router as companies_router
from app.api.discovery import router as discovery_router
from app.api.excluded_domains import router as excluded_domains_router
from app.api.gmail import router as gmail_router
from app.api.email_campaigns import router as email_campaigns_router
from app.api.email_suppression import router as email_suppression_router
from app.api.email_templates import router as email_templates_router
from app.api.pagespeed import router as pagespeed_router
from app.api.push_notifications import router as push_notifications_router
from app.api.user_preferences import router as user_preferences_router
from app.api.queue_events import router as queue_events_router
from app.api.overview import router as overview_router
from app.api.settings import router as settings_router
from app.api.google_oauth import router as google_oauth_router
from app.api.websites import router as websites_router
from app.api.website_crawler import router as website_crawler_router
from app.api.internal_worker import router as internal_worker_router

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
async def lifespan(app: FastAPI):
    validate_auth_configuration()
    if should_auto_migrate() and command is not None and Config is not None:
        alembic_cfg = Config("alembic.ini")
        logger.info("Starting database migrations.")
        command.upgrade(alembic_cfg, "head")
        logger.info("Database migrations completed.")

    http_client_pool = HTTPClientPool()
    app.state.http_client_pool = http_client_pool
    pool_token = install_http_client_pool(http_client_pool)
    worker_task: asyncio.Task[None] | None = None
    try:
        if settings.run_worker_in_api:
            from app.worker import run_worker

            logger.info("Starting the background worker inside the API process.")
            worker_task = asyncio.create_task(
                run_worker(),
                name="website-crawl-and-pagespeed-worker",
            )
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task
        reset_http_client_pool(pool_token)
        await http_client_pool.aclose()


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
    media_router,
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

app.include_router(
    email_campaigns_router,
    prefix="/api/v1",
)

app.include_router(
    email_suppression_router,
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

app.include_router(
    excluded_domains_router,
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
    push_notifications_router,
    prefix="/api/v1",
)

app.include_router(
    user_preferences_router,
    prefix="/api/v1",
)

app.include_router(
    overview_router,
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

app.include_router(internal_worker_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {
        "status": "ok",
        "environment": settings.environment,
    }
