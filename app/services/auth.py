import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import parse_token_expiry, settings
from app.models import AuthRefreshSession, Organization, User
from app.schemas.auth import CredentialsInput, OrganizationRegistration
from app.tenant_context import TenantContext


ACCESS_TOKEN_COOKIE = "lead_intelligence_access"
REFRESH_TOKEN_COOKIE = "lead_intelligence_refresh"
AUTH_COOKIE_PATH = "/api/v1/auth"
PASSWORD_HASH_ITERATIONS = 310_000


def create_bootstrap_admin(
    payload: OrganizationRegistration,
    db: Session,
) -> User:
    organization = db.scalar(
        select(Organization)
        .order_by(Organization.created_at)
        .limit(1)
        .with_for_update()
    )
    if db.scalar(select(func.count()).select_from(User)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Initial administrator setup has already been completed.",
        )
    if organization is None:
        organization = Organization(name=payload.organization_name.strip())
        db.add(organization)
        db.flush()
    else:
        organization.name = payload.organization_name.strip()

    user = _create_user(
        db,
        organization_id=organization.id,
        email=payload.email,
        password=payload.password,
        role="admin",
    )
    db.refresh(user)
    return user


def register_organization(
    payload: OrganizationRegistration,
    db: Session,
) -> User:
    organization = Organization(name=payload.organization_name.strip())
    db.add(organization)
    db.flush()
    return _create_user(
        db,
        organization_id=organization.id,
        email=payload.email,
        password=payload.password,
        role="admin",
    )


def create_organization_user(
    *,
    organization_id: UUID,
    email: str,
    password: str,
    role: str,
    db: Session,
) -> User:
    return _create_user(
        db,
        organization_id=organization_id,
        email=email,
        password=password,
        role=role,
    )


def _create_user(
    db: Session,
    *,
    organization_id: UUID,
    email: str,
    password: str,
    role: str,
) -> User:
    user = User(
        organization_id=organization_id,
        email=email,
        password_hash=hash_password(password),
        role=role,
    )
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        ) from exc
    return user


def authenticate(email: str, password: str, db: Session) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if (
        user is None
        or not user.is_active
        or not verify_password(password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is incorrect.",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return user


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived_key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return "$".join(
        (
            str(PASSWORD_HASH_ITERATIONS),
            _base64_encode(salt),
            _base64_encode(derived_key),
        )
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        iterations_raw, salt_raw, expected_raw = password_hash.split("$", 2)
        iterations = int(iterations_raw)
        if not 100_000 <= iterations <= 2_000_000:
            return False
        expected = _base64_decode(expected_raw)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _base64_decode(salt_raw),
            iterations,
        )
    except (ValueError, UnicodeEncodeError):
        return False
    return hmac.compare_digest(actual, expected)


def create_auth_tokens(user: User, db: Session) -> tuple[str, str]:
    issued_at = int(time.time())
    refresh_id = secrets.token_urlsafe(32)
    access_duration = access_token_duration_seconds()
    refresh_duration = refresh_token_duration_seconds()
    expires_at = datetime.fromtimestamp(
        issued_at + refresh_duration,
        tz=timezone.utc,
    )
    db.execute(
        delete(AuthRefreshSession).where(
            AuthRefreshSession.expires_at <= datetime.now(timezone.utc)
        )
    )
    db.add(
        AuthRefreshSession(
            user_id=user.id,
            token_hash=_hash_refresh_id(refresh_id),
            expires_at=expires_at,
        )
    )
    db.commit()
    return (
        _issue_token(
            user,
            token_type="access",
            expires_at=issued_at + access_duration,
        ),
        _issue_token(
            user,
            token_type="refresh",
            expires_at=issued_at + refresh_duration,
            token_id=refresh_id,
        ),
    )


def rotate_refresh_token(token: str, db: Session) -> tuple[User, str, str]:
    payload = _decode_token(token, expected_type="refresh")
    token_id = payload.get("jti")
    if not isinstance(token_id, str):
        raise _invalid_refresh_token()

    token_hash = _hash_refresh_id(token_id)
    refresh_session = db.scalar(
        select(AuthRefreshSession)
        .where(AuthRefreshSession.token_hash == token_hash)
        .with_for_update()
    )
    if refresh_session is None:
        raise _invalid_refresh_token()

    consumed = db.execute(
        delete(AuthRefreshSession).where(
            AuthRefreshSession.id == refresh_session.id,
        )
    )
    if consumed.rowcount != 1:
        db.rollback()
        raise _invalid_refresh_token()

    user = _user_from_token_payload(payload, db)
    if user is None:
        db.rollback()
        raise _invalid_refresh_token()
    access_token, refresh_token = create_auth_tokens(user, db)
    return user, access_token, refresh_token


def _issue_token(
    user: User,
    *,
    token_type: str,
    expires_at: int,
    token_id: str | None = None,
) -> str:
    secret = _token_secret(token_type)
    payload = {
        "sub": str(user.id),
        "org": str(user.organization_id),
        "ver": user.auth_version,
        "exp": expires_at,
    }
    if token_type is not None:
        payload["typ"] = token_type
    if token_id is not None:
        payload["jti"] = token_id
    encoded_payload = _base64_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    signature = hmac.new(
        secret,
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_base64_encode(signature)}"


def validate_access_token(token: str, db: Session) -> TenantContext | None:
    try:
        payload = _decode_token(
            token,
            expected_type="access",
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return None
        raise
    user = _user_from_token_payload(payload, db)
    if user is None:
        return None
    return TenantContext(
        organization_id=user.organization_id,
        user_id=user.id,
        email=user.email,
        role=user.role,
    )


def revoke_refresh_token(token: str, db: Session) -> None:
    try:
        payload = _decode_token(token, expected_type="refresh")
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return
        raise
    token_id = payload.get("jti")
    if not isinstance(token_id, str):
        return
    db.execute(
        delete(AuthRefreshSession).where(
            AuthRefreshSession.token_hash == _hash_refresh_id(token_id),
        )
    )
    db.commit()


def revoke_user_refresh_tokens(user_id: UUID, db: Session) -> None:
    db.execute(
        delete(AuthRefreshSession).where(
            AuthRefreshSession.user_id == user_id,
        )
    )


def _decode_token(
    token: str,
    *,
    expected_type: str,
) -> dict[str, object]:
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = json.loads(_base64_decode(encoded_payload))
        if not isinstance(payload, dict):
            raise ValueError("Invalid token payload.")
        if int(payload["exp"]) <= int(time.time()):
            raise ValueError("Expired token.")
        if payload.get("typ") != expected_type:
            raise ValueError("Unexpected token type.")
        encoded_signature_bytes = _base64_decode(encoded_signature)
        signed = encoded_payload.encode("ascii")
        expected_signature = hmac.new(
            _token_secret(expected_type),
            signed,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(
            encoded_signature_bytes,
            expected_signature,
        ):
            raise ValueError("Invalid token signature.")
        return payload
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise _invalid_refresh_token() from exc


def _user_from_token_payload(
    payload: dict[str, object],
    db: Session,
) -> User | None:
    try:
        user_id = UUID(str(payload["sub"]))
        organization_id = UUID(str(payload["org"]))
        auth_version = int(payload["ver"])
    except (ValueError, KeyError, TypeError):
        return None
    user = db.get(User, user_id)
    if (
        user is None
        or not user.is_active
        or user.organization_id != organization_id
        or user.auth_version != auth_version
    ):
        return None
    return user


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
) -> None:
    secure = _cookies_secure()
    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        access_token,
        max_age=access_token_duration_seconds(),
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        REFRESH_TOKEN_COOKIE,
        refresh_token,
        max_age=refresh_token_duration_seconds(),
        httponly=True,
        secure=secure,
        samesite="lax",
        path=AUTH_COOKIE_PATH,
    )


def clear_auth_cookies(response: Response) -> None:
    secure = _cookies_secure()
    for cookie_name, path in (
        (ACCESS_TOKEN_COOKIE, "/"),
        (REFRESH_TOKEN_COOKIE, AUTH_COOKIE_PATH),
    ):
        response.delete_cookie(
            cookie_name,
            httponly=True,
            secure=secure,
            samesite="lax",
            path=path,
        )


def clear_session_cookie(response: Response) -> None:
    """Compatibility helper for code that still uses the former name."""
    clear_auth_cookies(response)


def _cookies_secure() -> bool:
    return settings.environment.casefold() not in {
        "development",
        "dev",
        "test",
        "local",
    }


def _hash_refresh_id(token_id: str) -> str:
    return hashlib.sha256(token_id.encode("utf-8")).hexdigest()


def _invalid_refresh_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Your session has expired. Please sign in again.",
        headers={"WWW-Authenticate": "Cookie"},
    )


def validate_auth_configuration() -> None:
    access_secret = settings.access_token_secret
    refresh_secret = settings.refresh_token_secret
    if bool(access_secret) != bool(refresh_secret):
        raise _auth_configuration_error(
            "ACCESS_TOKEN_SECRET and REFRESH_TOKEN_SECRET must both be configured."
        )
    access_token_duration_seconds()
    refresh_token_duration_seconds()
    if access_secret and refresh_secret:
        access_key = _decode_configured_secret(
            access_secret,
            "ACCESS_TOKEN_SECRET",
        )
        refresh_key = _decode_configured_secret(
            refresh_secret,
            "REFRESH_TOKEN_SECRET",
        )
        if hmac.compare_digest(access_key, refresh_key):
            raise _auth_configuration_error(
                "Access and refresh token secrets must be different."
            )
        return
    raise _auth_configuration_error(
        "Configure distinct ACCESS_TOKEN_SECRET and REFRESH_TOKEN_SECRET values."
    )


def access_token_duration_seconds() -> int:
    try:
        return parse_token_expiry(
            settings.access_token_expiry,
            maximum_seconds=24 * 60 * 60,
        )
    except ValueError as exc:
        raise _auth_configuration_error(
            "ACCESS_TOKEN_EXPIRY must be a positive duration no longer than 24 hours."
        ) from exc


def refresh_token_duration_seconds() -> int:
    try:
        return parse_token_expiry(
            settings.refresh_token_expiry,
            maximum_seconds=365 * 24 * 60 * 60,
        )
    except ValueError as exc:
        raise _auth_configuration_error(
            "REFRESH_TOKEN_EXPIRY must be a positive duration no longer than 365 days."
        ) from exc


def _token_secret(token_type: str) -> bytes:
    if token_type == "access":
        configured = settings.access_token_secret
        setting_name = "ACCESS_TOKEN_SECRET"
    elif token_type == "refresh":
        configured = settings.refresh_token_secret
        setting_name = "REFRESH_TOKEN_SECRET"
    else:
        raise ValueError(f"Unsupported auth token type: {token_type}")
    if not configured:
        raise _auth_configuration_error(
            f"{setting_name} is required."
        )
    return _decode_configured_secret(configured, setting_name)


def _decode_configured_secret(value: str, setting_name: str) -> bytes:
    try:
        secret = bytes.fromhex(value)
    except ValueError as exc:
        raise _auth_configuration_error(
            f"{setting_name} must be a hexadecimal secret."
        ) from exc
    if len(secret) < 32:
        raise _auth_configuration_error(
            f"{setting_name} must contain at least 32 random bytes."
        )
    return secret


def _auth_configuration_error(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
    )


def _base64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
