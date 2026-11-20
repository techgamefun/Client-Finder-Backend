import base64
import binascii
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=256)
    auth: str = Field(min_length=1, max_length=256)

    @field_validator("p256dh")
    @classmethod
    def validate_p256dh(cls, value: str) -> str:
        decoded = _decode_subscription_key(value)
        if len(decoded) != 65 or decoded[0] != 4:
            raise ValueError("p256dh must be an uncompressed P-256 public key.")
        return value

    @field_validator("auth")
    @classmethod
    def validate_auth(cls, value: str) -> str:
        if len(_decode_subscription_key(value)) != 16:
            raise ValueError("auth must be a 16-byte Web Push authentication secret.")
        return value


def _decode_subscription_key(value: str) -> bytes:
    padded = value + ("=" * ((4 - len(value) % 4) % 4))
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Subscription keys must be base64url encoded.") from exc


class PushSubscriptionCreate(BaseModel):
    endpoint: str = Field(min_length=8, max_length=2048)
    keys: PushSubscriptionKeys

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Push endpoint URL is invalid.") from exc

        supported_host = host in {
            "fcm.googleapis.com",
            "updates.push.services.mozilla.com",
            "web.push.apple.com",
            "push.apple.com",
        } or (host is not None and host.endswith(".notify.windows.com"))
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or port not in (None, 443)
            or not supported_host
        ):
            raise ValueError("Push endpoint must use a supported HTTPS push service.")
        return value


class PushSubscriptionDelete(BaseModel):
    endpoint: str = Field(min_length=8, max_length=2048)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if urlsplit(value).scheme != "https":
            raise ValueError("Push endpoints must use HTTPS.")
        return value


class PushSubscriptionResponse(BaseModel):
    id: UUID
    endpoint: str
    created_at: datetime


class PushPreferences(BaseModel):
    website_crawl_enabled: bool = True
    pagespeed_enabled: bool = True
    outreach_enabled: bool = True


class PushPreferencesUpdate(BaseModel):
    website_crawl_enabled: bool
    pagespeed_enabled: bool
    outreach_enabled: bool


class PushSettingsResponse(BaseModel):
    configured: bool
    public_key: str | None
    subscriptions: list[PushSubscriptionResponse]
    preferences: PushPreferences


class PushSubscriptionDeleteResponse(BaseModel):
    removed: bool

