import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from email.headerregistry import Address
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SuppressedContact


_TOKEN_VERSION = "v1"
_MAX_TOKEN_LENGTH = 16_384
_DOMAIN_PATTERN = re.compile(r"^[a-z0-9.-]+$")
_VALID_REASONS = {"unsubscribe", "hard_bounce", "complaint", "manual_dnc"}


@dataclass(frozen=True)
class UnsubscribeClaims:
    organization_id: UUID
    email: str | None
    permitted_email_hashes: tuple[str, ...] | None = None


def normalize_email(value: str) -> str:
    try:
        address = Address(addr_spec=value.strip())
        local_part = address.username
        domain = address.domain.encode("idna").decode("ascii").lower().rstrip(".")
    except (UnicodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a valid email address.",
        ) from exc
    if not local_part or not domain or "@" in local_part:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a valid email address.",
        )
    return f"{local_part.lower()}@{domain}"


def normalize_domain(value: str) -> str:
    candidate = value.strip().lower().lstrip("@").rstrip(".")
    try:
        domain = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a valid domain name.",
        ) from exc
    labels = domain.split(".")
    if (
        not domain
        or "." not in domain
        or len(domain) > 253
        or not _DOMAIN_PATTERN.fullmatch(domain)
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            for label in labels
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a valid domain name.",
        )
    return domain


def create_unsubscribe_token(
    organization_id: UUID,
    email: str | None = None,
    *,
    permitted_emails: list[str] | None = None,
) -> str:
    if permitted_emails is not None:
        normalized_emails = sorted({normalize_email(item) for item in permitted_emails})
        if not normalized_emails:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="At least one recipient is required for this unsubscribe link.",
            )
        recipient_hashes = sorted(_email_hash(item) for item in normalized_emails)
        encrypted = Fernet(_encryption_key()).encrypt(
            json.dumps(
                {
                    "org": str(organization_id),
                    "recipient_hashes": recipient_hashes,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        return f"v1e.{encrypted.decode('ascii').rstrip('=')}"

    claims = {
        "email": normalize_email(email) if email is not None else None,
        "org": str(organization_id),
    }
    payload = _encode_base64(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signed_value = f"{_TOKEN_VERSION}.{payload}"
    signature = _encode_base64(
        hmac.new(_signing_key(), signed_value.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{signed_value}.{signature}"


def verify_unsubscribe_token(token: str) -> UnsubscribeClaims:
    try:
        if len(token) > _MAX_TOKEN_LENGTH:
            raise ValueError("Token is too long.")
        segments = token.split(".")
        if len(segments) == 2 and segments[0] == "v1e":
            encoded = segments[1]
            payload = Fernet(_encryption_key()).decrypt(
                (encoded + "=" * (-len(encoded) % 4)).encode("ascii")
            )
            claims = json.loads(payload)
            organization_id = UUID(claims["org"])
            permitted_email_hashes = tuple(
                claims["recipient_hashes"]
            )
            if not permitted_email_hashes or any(
                not isinstance(value, str) or len(value) != 64
                for value in permitted_email_hashes
            ):
                raise ValueError("No recipient addresses are allowed.")
            return UnsubscribeClaims(
                organization_id=organization_id,
                email=None,
                permitted_email_hashes=permitted_email_hashes,
            )

        if len(segments) != 3:
            raise ValueError("Malformed token.")
        version, payload, signature = segments
        if version != _TOKEN_VERSION:
            raise ValueError("Unsupported token version.")
        signed_value = f"{version}.{payload}"
        expected = hmac.new(
            _signing_key(),
            signed_value.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_decode_base64(signature), expected):
            raise ValueError("Invalid signature.")
        claims = json.loads(_decode_base64(payload))
        organization_id = UUID(claims["org"])
        claim_email = claims["email"]
        email = normalize_email(claim_email) if claim_email is not None else None
    except (
        binascii.Error,
        InvalidToken,
        KeyError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This unsubscribe link is invalid or has been altered.",
        ) from exc
    return UnsubscribeClaims(
        organization_id=organization_id,
        email=email,
    )


def build_unsubscribe_url(
    organization_id: UUID,
    email: str | None = None,
    *,
    one_click: bool = False,
    permitted_emails: list[str] | None = None,
) -> str:
    token = create_unsubscribe_token(
        organization_id,
        email,
        permitted_emails=permitted_emails,
    )
    frontend_origin = settings.frontend_origins.split(",", maxsplit=1)[0].strip()
    if not frontend_origin:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The public unsubscribe page is not configured.",
        )
    path = "/unsubscribe/one-click" if one_click else "/unsubscribe"
    if one_click and email is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="One-click unsubscribe links require a recipient address.",
        )
    return f"{frontend_origin.rstrip('/')}{path}?token={token}"


def unsubscribe_email(
    claims: UnsubscribeClaims,
    email: str | None = None,
) -> str:
    if claims.email is not None:
        if email is not None and normalize_email(email) != claims.email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The email address does not match this unsubscribe link.",
            )
        return claims.email
    if email is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter the email address that received this message.",
        )
    normalized_email = normalize_email(email)
    if (
        claims.permitted_email_hashes is not None
        and not any(
            hmac.compare_digest(_email_hash(normalized_email), allowed_hash)
            for allowed_hash in claims.permitted_email_hashes
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That address was not a recipient of the message.",
        )
    return normalized_email


def is_email_suppressed(db: Session, organization_id: UUID, email: str) -> bool:
    return email_suppression_reasons(db, organization_id, [email]).get(
        normalize_email(email)
    ) is not None


def email_suppression_reasons(
    db: Session,
    organization_id: UUID,
    emails: list[str],
) -> dict[str, str]:
    normalized_emails = {normalize_email(email) for email in emails}
    if not normalized_emails:
        return {}
    domains = {email.rsplit("@", maxsplit=1)[1] for email in normalized_emails}
    domain_suffixes: set[str] = set()
    for domain in domains:
        labels = domain.split(".")
        for index in range(len(labels)):
            domain_suffixes.add(".".join(labels[index:]))
    conditions = [
        (SuppressedContact.target_type == "email")
        & (SuppressedContact.normalized_value.in_(normalized_emails)),
    ]
    if domain_suffixes:
        conditions.append(
            (SuppressedContact.target_type == "domain")
            & (SuppressedContact.normalized_value.in_(domain_suffixes))
        )
    records = db.scalars(
        select(SuppressedContact).where(
            or_(
                SuppressedContact.organization_id == organization_id,
                SuppressedContact.global_dnc.is_(True),
            ),
            or_(*conditions),
        )
    ).all()
    direct_suppressions = {
        record.normalized_value: record.reason
        for record in records
        if record.target_type == "email"
    }
    domain_suppressions = [
        (record.normalized_value, record.reason)
        for record in records
        if record.target_type == "domain"
    ]
    result: dict[str, str] = {}
    for email in normalized_emails:
        domain = email.rsplit("@", maxsplit=1)[1]
        reason = direct_suppressions.get(email)
        if reason is None:
            for suppressed_domain, domain_reason in domain_suppressions:
                if domain == suppressed_domain or domain.endswith(
                    f".{suppressed_domain}"
                ):
                    reason = domain_reason
                    break
        if reason is not None:
            result[email] = reason
    return result


def add_suppression(
    db: Session,
    *,
    organization_id: UUID | None,
    target_type: str,
    value: str,
    reason: str,
    created_by_user_id: UUID | None = None,
) -> SuppressedContact:
    if target_type not in {"email", "domain"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Target type must be email or domain.",
        )
    if reason not in _VALID_REASONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Select a valid suppression reason.",
        )
    normalized_value = (
        normalize_email(value) if target_type == "email" else normalize_domain(value)
    )
    statement = select(SuppressedContact).where(
        SuppressedContact.organization_id == organization_id,
        SuppressedContact.target_type == target_type,
        SuppressedContact.normalized_value == normalized_value,
        SuppressedContact.reason == reason,
    )
    existing = db.scalar(statement)
    if existing is not None:
        return existing
    record = SuppressedContact(
        organization_id=organization_id,
        target_type=target_type,
        normalized_value=normalized_value,
        reason=reason,
        global_dnc=True,
        created_by_user_id=created_by_user_id,
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(statement)
        if existing is not None:
            return existing
        raise
    return record


def _signing_key() -> bytes:
    configured_secret = settings.email_unsubscribe_secret
    if configured_secret:
        return configured_secret.encode("utf-8")
    access_token_secret = settings.access_token_secret
    if not access_token_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The email unsubscribe signing secret is not configured.",
        )
    return hmac.new(
        access_token_secret.encode("utf-8"),
        b"email-unsubscribe-v1",
        hashlib.sha256,
    ).digest()


def _encryption_key() -> bytes:
    return base64.urlsafe_b64encode(
        hmac.new(
            _signing_key(),
            b"email-unsubscribe-recipient-list",
            hashlib.sha256,
        ).digest()
    )


def _email_hash(email: str) -> str:
    return hmac.new(
        _signing_key(),
        email.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_base64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
