import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from app.config import settings
from app.http_clients import async_http_client


GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
OAUTH_STATE_TTL_SECONDS = 600
GMAIL_SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
)


@dataclass(frozen=True)
class GoogleOAuthState:
    org_id: UUID
    user_id: UUID
    nonce: str


@dataclass(frozen=True)
class GoogleOAuthTokens:
    access_token: str
    refresh_token: str | None


@dataclass(frozen=True)
class GoogleProfile:
    google_account_id: str
    email_address: str


def create_google_authorization_url(
    org_id: UUID,
    user_id: UUID,
) -> tuple[str, str]:
    client_id, _, redirect_uri, state_secret = _oauth_configuration()
    nonce = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "org_id": str(org_id),
            "user_id": str(user_id),
            "nonce": nonce,
            "exp": int(time.time()) + OAUTH_STATE_TTL_SECONDS,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded_payload = _base64url_encode(payload)
    signature = hmac.new(
        state_secret,
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    state = f"{encoded_payload}.{_base64url_encode(signature)}"
    parameters = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GMAIL_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"{GOOGLE_AUTHORIZE_URL}?{parameters}", nonce


def decode_google_oauth_state(state: str) -> GoogleOAuthState:
    _, _, _, state_secret = _oauth_configuration()
    try:
        encoded_payload, encoded_signature = state.split(".", maxsplit=1)
        provided_signature = _base64url_decode(encoded_signature)
        expected_signature = hmac.new(
            state_secret,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(provided_signature, expected_signature):
            raise ValueError("Invalid OAuth state signature.")
        payload = json.loads(_base64url_decode(encoded_payload))
        org_id = UUID(payload["org_id"])
        user_id = UUID(payload["user_id"])
        nonce = payload["nonce"]
        expires_at = payload["exp"]
        if (
            not isinstance(nonce, str)
            or len(nonce) < 32
            or not isinstance(expires_at, int)
            or expires_at <= int(time.time())
        ):
            raise ValueError("Invalid or expired OAuth state.")
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The Google OAuth state is invalid or expired.",
        ) from exc
    return GoogleOAuthState(
        org_id=org_id,
        user_id=user_id,
        nonce=nonce,
    )


async def exchange_google_authorization_code(code: str) -> GoogleOAuthTokens:
    client_id, client_secret, redirect_uri, _ = _oauth_configuration()
    try:
        async with async_http_client(timeout=20.0) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google authorization could not be completed.",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google returned an invalid token response.",
        )
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google did not return an access token.",
        )
    if refresh_token is not None and not isinstance(refresh_token, str):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google returned an invalid refresh token.",
        )
    return GoogleOAuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
    )


async def fetch_google_profile(access_token: str) -> GoogleProfile:
    try:
        async with async_http_client(timeout=20.0) as client:
            response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The connected Google account profile could not be loaded.",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google returned an invalid account profile.",
        )
    google_account_id = payload.get("sub")
    email_address = payload.get("email")
    if (
        not isinstance(google_account_id, str)
        or not google_account_id
        or not isinstance(email_address, str)
        or not email_address
        or payload.get("email_verified") is not True
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Google did not return a verified account email.",
        )
    return GoogleProfile(
        google_account_id=google_account_id,
        email_address=email_address,
    )


def _oauth_configuration() -> tuple[str, str, str, bytes]:
    client_id = settings.google_oauth_client_id
    client_secret = settings.google_oauth_client_secret
    redirect_uri = settings.google_oauth_redirect_uri
    state_secret = settings.google_oauth_state_secret
    if not client_id or not client_secret or not redirect_uri or not state_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not fully configured on the server.",
        )
    try:
        state_secret_bytes = state_secret.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth state signing is misconfigured.",
        ) from exc
    if len(state_secret_bytes) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GOOGLE_OAUTH_STATE_SECRET must contain at least 32 bytes.",
        )
    return client_id, client_secret, redirect_uri, state_secret_bytes


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
