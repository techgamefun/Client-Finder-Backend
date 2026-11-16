from typing import Literal

from app.config import settings


GMAIL_OAUTH_STATE_COOKIE = "gmail_oauth_nonce"
GMAIL_CALLBACK_PATH = "/"


def gmail_oauth_cookie_security() -> tuple[bool, Literal["lax", "none"]]:
    secure = settings.environment == "production"
    return secure, "none" if secure else "lax"
