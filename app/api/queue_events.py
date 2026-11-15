import asyncio
import json
import logging

import psycopg
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.engine import make_url
from starlette.websockets import WebSocketState

from app.database import SessionLocal, database_url
from app.services.auth import (
    ACCESS_TOKEN_COOKIE,
    validate_access_token,
)
from app.tenant_context import reset_tenant_context, set_tenant_context


logger = logging.getLogger(__name__)
router = APIRouter()


async def _connect_to_queue_events() -> psycopg.AsyncConnection:
    dsn = make_url(database_url).set(drivername="postgresql").render_as_string(
        hide_password=False,
    )
    return await psycopg.AsyncConnection.connect(dsn, autocommit=True)


async def _forward_notifications(
    websocket: WebSocket,
    connection: psycopg.AsyncConnection,
    organization_id: str,
) -> None:
    async for notification in connection.notifies():
        try:
            event = json.loads(notification.payload)
        except json.JSONDecodeError:
            logger.error("Received an invalid queue notification payload.")
            continue
        if (
            not isinstance(event, dict)
            or event.get("organization_id") != organization_id
        ):
            continue
        event.pop("organization_id", None)
        await websocket.send_text(json.dumps(event))


async def _wait_for_disconnect(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


@router.websocket("/queue/events")
async def queue_job_events(websocket: WebSocket) -> None:
    token = websocket.cookies.get(ACCESS_TOKEN_COOKIE)
    if not token:
        await websocket.close(code=4401, reason="Authentication is required.")
        return
    session_factory = getattr(
        websocket.app.state,
        "session_factory",
        SessionLocal,
    )
    with session_factory() as db:
        context = validate_access_token(token, db)
    if context is None:
        await websocket.close(code=4401, reason="Your session is invalid.")
        return

    tenant_token = set_tenant_context(context)
    await websocket.accept()

    try:
        async with await _connect_to_queue_events() as connection:
            await connection.execute("LISTEN queue_job_events")
            notification_task = asyncio.create_task(
                _forward_notifications(
                    websocket,
                    connection,
                    str(context.organization_id),
                ),
            )
            disconnect_task = asyncio.create_task(
                _wait_for_disconnect(websocket),
            )
            done, pending = await asyncio.wait(
                {notification_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for task in done:
                task.result()
    except WebSocketDisconnect:
        return
    except psycopg.Error:
        logger.exception("PostgreSQL queue event listener failed.")
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.close(
                code=1011,
                reason="Queue events are unavailable.",
            )
    finally:
        reset_tenant_context(tenant_token)
