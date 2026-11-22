import asyncio

from app import main, worker
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


def test_lifespan_starts_and_stops_in_process_worker(monkeypatch):
    monkeypatch.setattr(main.settings, "run_worker_in_api", True)
    monkeypatch.setattr(main, "should_auto_migrate", lambda: False)

    async def exercise_lifespan():
        worker_started = asyncio.Event()
        worker_stopped = asyncio.Event()

        async def fake_worker():
            worker_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                worker_stopped.set()

        monkeypatch.setattr(worker, "run_worker", fake_worker)

        async with main.lifespan(main.app):
            await asyncio.wait_for(worker_started.wait(), timeout=1)
            assert not worker_stopped.is_set()

        assert worker_stopped.is_set()

    asyncio.run(exercise_lifespan())
