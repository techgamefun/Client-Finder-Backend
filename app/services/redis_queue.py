import httpx

from app.config import settings


QUEUE_KEY = "website-crawl:jobs"


class RedisQueueError(RuntimeError):
    pass


def validate_redis_configuration() -> tuple[str, str]:
    url = settings.upstash_redis_rest_url
    token = settings.upstash_redis_rest_token
    if not url or not token:
        raise RedisQueueError(
            "UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN must be configured."
        )
    return url.rstrip("/"), token


async def _execute(*command: str) -> object:
    url, token = validate_redis_configuration()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=list(command),
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RedisQueueError("The Redis queue request failed.") from exc

    if not isinstance(payload, dict):
        raise RedisQueueError("The Redis queue returned an invalid response.")
    if payload.get("error"):
        raise RedisQueueError("The Redis queue rejected the command.")
    return payload.get("result")


async def enqueue_job(job_id: str) -> None:
    await _execute("LPUSH", QUEUE_KEY, job_id)


async def dequeue_job() -> str | None:
    result = await _execute("RPOP", QUEUE_KEY)
    if result is None:
        return None
    if not isinstance(result, str):
        raise RedisQueueError("The Redis queue returned an invalid job ID.")
    return result
