import json
import logging
from uuid import UUID

import httpx

from app.config import settings
from app.http_clients import sync_http_client


logger = logging.getLogger(__name__)
_CACHE_TTL_SECONDS = 180
_CACHE_KEY_PREFIX = "user-preferences:v1"


def read_cached_preferences(
    organization_id: UUID,
    user_id: UUID,
) -> dict[str, object] | None:
    if not _is_configured():
        return None

    try:
        result = _execute("GET", _cache_key(organization_id, user_id))
    except RuntimeError:
        logger.warning("Redis preference cache read failed; using the database.")
        return None

    if result is None:
        return None
    if not isinstance(result, str):
        logger.warning("Redis preference cache returned an invalid value.")
        return None
    try:
        preferences = json.loads(result)
    except json.JSONDecodeError:
        logger.warning("Redis preference cache returned invalid JSON.")
        return None
    if not isinstance(preferences, dict):
        logger.warning("Redis preference cache returned an invalid object.")
        return None
    return preferences


def write_cached_preferences(
    organization_id: UUID,
    user_id: UUID,
    preferences: dict[str, object],
) -> None:
    if not _is_configured():
        return
    try:
        _execute(
            "SET",
            _cache_key(organization_id, user_id),
            json.dumps(preferences, separators=(",", ":")),
            "EX",
            str(_CACHE_TTL_SECONDS),
        )
    except RuntimeError:
        logger.warning("Redis preference cache write failed.")


def invalidate_cached_preferences(
    organization_id: UUID,
    user_id: UUID,
) -> None:
    if not _is_configured():
        return
    try:
        _execute("DEL", _cache_key(organization_id, user_id))
    except RuntimeError:
        logger.warning("Redis preference cache invalidation failed.")


def _cache_key(organization_id: UUID, user_id: UUID) -> str:
    return f"{_CACHE_KEY_PREFIX}:{organization_id}:{user_id}"


def _is_configured() -> bool:
    return bool(
        settings.upstash_redis_rest_url
        and settings.upstash_redis_rest_token
    )


def _execute(*command: str) -> object:
    url = settings.upstash_redis_rest_url
    token = settings.upstash_redis_rest_token
    if not url or not token:
        raise RuntimeError("Redis preference cache is not configured.")

    try:
        with sync_http_client(timeout=1.0) as client:
            response = client.post(
                f"{url.rstrip('/')}",
                headers={"Authorization": f"Bearer {token}"},
                json=list(command),
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("Redis preference cache request failed.") from exc

    if not isinstance(payload, dict) or payload.get("error"):
        raise RuntimeError("Redis preference cache rejected the command.")
    return payload.get("result")
