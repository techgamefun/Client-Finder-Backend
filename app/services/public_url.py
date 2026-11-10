import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException, status


def _normalize_website_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A public HTTP or HTTPS website URL is required.",
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The website URL contains an invalid port.",
        ) from exc

    expected_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != expected_port:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only standard HTTP and HTTPS ports are supported.",
        )

    host = parsed.hostname.lower()
    netloc = host
    if ":" in host:
        netloc = f"[{host}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))


def _assert_public_host(host: str) -> None:
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Private and local website addresses cannot be crawled.",
            )
        try:
            addresses = [
                ipaddress.ip_address(info[4][0])
                for info in socket.getaddrinfo(
                    host,
                    None,
                    type=socket.SOCK_STREAM,
                )
            ]
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The website hostname could not be resolved.",
            ) from exc

    if not addresses or any(not address.is_global for address in addresses):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Private and non-public website addresses cannot be crawled.",
        )


async def _validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only public HTTP or HTTPS website URLs can be crawled.",
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The website URL contains an invalid port.",
        ) from exc
    expected_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != expected_port:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only standard HTTP and HTTPS ports are supported.",
        )
    await asyncio.to_thread(_assert_public_host, parsed.hostname)
