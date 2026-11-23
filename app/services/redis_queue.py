import httpx

from app.config import settings
from app.http_clients import async_http_client


QUEUE_KEY = "website-crawl:jobs"
PAGESPEED_QUEUE_KEY = "pagespeed-scan:jobs"
CORPUS_ANALYSIS_QUEUE_KEY = "website-corpus-analysis:jobs"
AHREFS_DOMAIN_RATING_QUEUE_KEY = "website-ahrefs-domain-rating:jobs"
HYPESTAT_FETCH_QUEUE_KEY = "website-hypestat-fetch:jobs"


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
        async with async_http_client(timeout=10.0) as client:
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
    error = payload.get("error")
    if error:
        if isinstance(error, str):
            detail = " ".join(error.split())[:300]
            raise RedisQueueError(
                f"The Redis queue rejected the command: {detail}"
            )
        raise RedisQueueError("The Redis queue rejected the command.")
    return payload.get("result")


async def enqueue_job(job_id: str) -> None:
    await _execute("LPUSH", QUEUE_KEY, job_id)


async def enqueue_crawl_jobs(job_ids: list[str]) -> None:
    if job_ids:
        await _execute("LPUSH", QUEUE_KEY, *job_ids)


async def enqueue_pagespeed_jobs(job_ids: list[str]) -> None:
    if job_ids:
        await _execute("LPUSH", PAGESPEED_QUEUE_KEY, *job_ids)


async def enqueue_corpus_analysis_jobs(job_ids: list[str]) -> None:
    if job_ids:
        await _execute("LPUSH", CORPUS_ANALYSIS_QUEUE_KEY, *job_ids)


async def enqueue_ahrefs_domain_rating_jobs(job_ids: list[str]) -> None:
    if job_ids:
        await _execute("LPUSH", AHREFS_DOMAIN_RATING_QUEUE_KEY, *job_ids)


async def enqueue_hypestat_fetch_jobs(job_ids: list[str]) -> None:
    if job_ids:
        await _execute("LPUSH", HYPESTAT_FETCH_QUEUE_KEY, *job_ids)


async def dequeue_job() -> str | None:
    return await _dequeue_from(QUEUE_KEY)


async def dequeue_pagespeed_job() -> str | None:
    return await _dequeue_from(PAGESPEED_QUEUE_KEY)


async def dequeue_corpus_analysis_job() -> str | None:
    return await _dequeue_from(CORPUS_ANALYSIS_QUEUE_KEY)


async def dequeue_ahrefs_domain_rating_job() -> str | None:
    return await _dequeue_from(AHREFS_DOMAIN_RATING_QUEUE_KEY)


async def dequeue_hypestat_fetch_job() -> str | None:
    return await _dequeue_from(HYPESTAT_FETCH_QUEUE_KEY)


async def _dequeue_from(queue_key: str) -> str | None:
    result = await _execute("RPOP", queue_key)
    if result is None:
        return None
    if not isinstance(result, str):
        raise RedisQueueError("The Redis queue returned an invalid job ID.")
    return result
