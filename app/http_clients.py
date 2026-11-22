from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from threading import Lock

import httpx


class HTTPClientPool:
    def __init__(self) -> None:
        self._async_clients: dict[str, httpx.AsyncClient] = {}
        self._sync_clients: dict[str, httpx.Client] = {}
        self._sync_clients_lock = Lock()

    def async_client(self, timeout: float | None) -> httpx.AsyncClient:
        key = repr(timeout)
        client = self._async_clients.get(key)
        if client is None:
            client = httpx.AsyncClient(timeout=timeout)
            self._async_clients[key] = client
        return client

    def sync_client(self, timeout: float | None) -> httpx.Client:
        key = repr(timeout)
        with self._sync_clients_lock:
            client = self._sync_clients.get(key)
            if client is None:
                client = httpx.Client(timeout=timeout)
                self._sync_clients[key] = client
            return client

    async def aclose(self) -> None:
        for client in self._async_clients.values():
            await client.aclose()
        for client in self._sync_clients.values():
            client.close()


_http_client_pool: ContextVar[HTTPClientPool | None] = ContextVar(
    "http_client_pool",
    default=None,
)


def install_http_client_pool(pool: HTTPClientPool) -> Token:
    return _http_client_pool.set(pool)


def reset_http_client_pool(token: Token) -> None:
    _http_client_pool.reset(token)


def current_http_client_pool() -> HTTPClientPool | None:
    return _http_client_pool.get()


@asynccontextmanager
async def async_http_client(
    timeout: float | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    pool = _http_client_pool.get()
    if pool is not None:
        yield pool.async_client(timeout)
        return

    async with httpx.AsyncClient(timeout=timeout) as client:
        yield client


@contextmanager
def sync_http_client(timeout: float | None = None) -> Iterator[httpx.Client]:
    pool = _http_client_pool.get()
    if pool is not None:
        yield pool.sync_client(timeout)
        return

    with httpx.Client(timeout=timeout) as client:
        yield client
