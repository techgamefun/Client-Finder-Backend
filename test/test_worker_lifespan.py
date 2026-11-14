import asyncio

from app import main, worker


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
