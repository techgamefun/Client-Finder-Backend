import asyncio

from app.http_clients import HTTPClientPool


def test_http_client_pool_reuses_clients_and_closes_them():
    async def exercise_pool():
        pool = HTTPClientPool()

        async_client = pool.async_client(timeout=10.0)
        sync_client = pool.sync_client(timeout=10.0)

        assert pool.async_client(timeout=10.0) is async_client
        assert pool.sync_client(timeout=10.0) is sync_client
        assert pool.async_client(timeout=20.0) is not async_client

        other_async_client = pool.async_client(timeout=20.0)
        await pool.aclose()

        assert async_client.is_closed
        assert other_async_client.is_closed
        assert sync_client.is_closed

    asyncio.run(exercise_pool())
