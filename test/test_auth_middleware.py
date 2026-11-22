import asyncio
import threading
from types import SimpleNamespace
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import Response

from app import auth_middleware
from app.tenant_context import TenantContext


def test_token_database_lookup_runs_off_the_event_loop(monkeypatch):
    event_loop_thread_id = threading.get_ident()
    validation_thread_ids = []
    context = TenantContext(
        organization_id=uuid4(),
        user_id=uuid4(),
        email="user@example.test",
        role="user",
    )

    class SessionContext:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def connection(self):
            return self

    def fake_validate_access_token(_token, _db):
        validation_thread_ids.append(threading.get_ident())
        return context

    monkeypatch.setattr(
        auth_middleware,
        "validate_access_token",
        fake_validate_access_token,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=lambda: SessionContext(),
            frontend_origins=[],
        )
    )
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/test",
            "raw_path": b"/api/v1/test",
            "query_string": b"",
            "headers": [
                (b"cookie", b"lead_intelligence_access=test-token"),
            ],
            "server": ("test", 80),
            "client": ("test", 1234),
            "root_path": "",
            "app": app,
        }
    )

    async def call_next(_request):
        return Response(status_code=200)

    response = asyncio.run(
        auth_middleware.authenticate_request(request, call_next)
    )

    assert response.status_code == 200
    assert validation_thread_ids
    assert validation_thread_ids[0] != event_loop_thread_id
    assert "auth;dur=" in response.headers["server-timing"]
    assert "db-checkout;dur=" in response.headers["server-timing"]
    assert "db;dur=" in response.headers["server-timing"]
