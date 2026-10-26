
from fastapi import FastAPI

from app.config import settings

from app.api.companies import router as companies_router
from app.api.discovery import router as discovery_router

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
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


@app.get("/health")
def health_check() -> dict[str, str]:
    return {
        "status": "ok",
        "environment": settings.environment,
    }
