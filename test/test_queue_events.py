import asyncio
import json
from types import SimpleNamespace

from app.api import queue_events


def test_queue_websocket_forwards_database_events(client, monkeypatch):
    organization_id = client.get("/api/v1/auth/me").json()["organization_id"]

    class FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def execute(self, query):
            assert query == "LISTEN queue_job_events"

        async def notifies(self):
            yield SimpleNamespace(
                payload=json.dumps(
                    {
                        "kind": "crawler",
                        "id": "job-id",
                        "status": "running",
                        "organization_id": organization_id,
                    }
                )
            )
            await asyncio.Event().wait()

    async def fake_connect():
        return FakeConnection()

    monkeypatch.setattr(
        queue_events,
        "_connect_to_queue_events",
        fake_connect,
    )

    with client.websocket_connect("/api/v1/queue/events") as websocket:
        assert websocket.receive_json() == {
            "kind": "crawler",
            "id": "job-id",
            "status": "running",
        }
