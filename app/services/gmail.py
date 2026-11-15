import base64
import binascii
import re
from dataclasses import dataclass
from email import policy
from email.headerregistry import Address
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses
from html.parser import HTMLParser
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status
from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ConnectedGmailAccount,
    UserGmailAssignment,
)
from app.schemas.gmail import (
    GmailMessageDetailResponse,
    GmailMessageSummary,
    GmailMessagesResponse,
    GmailReplyResponse,
)
from app.tenant_context import get_tenant_context


GMAIL_API_SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
)
MAX_GMAIL_BODY_CHARACTERS = 100_000
MESSAGE_METADATA_HEADERS = (
    "From",
    "Reply-To",
    "Subject",
    "Date",
    "Message-ID",
    "References",
)


@dataclass(frozen=True)
class GmailSendResult:
    message_id: str
    thread_id: str
    sender_email: str


def validate_gmail_token_encryption_configuration() -> None:
    _fernet()


def encrypt_refresh_token(refresh_token: str) -> str:
    return _fernet().encrypt(refresh_token.encode("utf-8")).decode("ascii")


def decrypt_refresh_token(encrypted_refresh_token: str) -> str:
    try:
        return (
            _fernet()
            .decrypt(encrypted_refresh_token.encode("ascii"))
            .decode("utf-8")
        )
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The saved Gmail refresh token cannot be decrypted. Verify GMAIL_TOKEN_ENCRYPTION_KEY.",
        ) from exc


def get_gmail_service(
    db: Session,
    gmail_account_id: UUID,
) -> Any:
    account = _get_assigned_gmail_account(db, gmail_account_id)
    return _build_gmail_service(account)


def get_gmail_account_email(
    db: Session,
    gmail_account_id: UUID,
) -> str:
    return _get_assigned_gmail_account(db, gmail_account_id).email_address


def send_gmail_message(
    db: Session,
    gmail_account_id: UUID,
    *,
    recipient_email: str,
    subject: str,
    body_content: str,
) -> GmailSendResult:
    account = _get_assigned_gmail_account(db, gmail_account_id)
    try:
        recipient = Address(addr_spec=recipient_email.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The selected contact does not have a valid email address.",
        ) from exc

    plain_text = _html_to_plain_text(body_content)
    message = EmailMessage(policy=policy.SMTP)
    message["To"] = recipient
    message["From"] = account.email_address
    message["Subject"] = subject
    message.set_content(plain_text)
    message.add_alternative(body_content, subtype="html")
    raw_message = (
        base64.urlsafe_b64encode(message.as_bytes())
        .decode("ascii")
        .rstrip("=")
    )
    response = _execute_google_request(
        _build_gmail_service(account)
        .users()
        .messages()
        .send(userId="me", body={"raw": raw_message}),
        missing_message_detail="The email could not be sent.",
    )
    return GmailSendResult(
        message_id=_required_string(response, "id"),
        thread_id=_required_string(response, "threadId"),
        sender_email=account.email_address,
    )


def _get_assigned_gmail_account(
    db: Session,
    gmail_account_id: UUID,
) -> ConnectedGmailAccount:
    context = get_tenant_context() or db.info.get("tenant_context")
    if context is None or context.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required to access Gmail.",
        )
    account = db.scalar(
        select(ConnectedGmailAccount).where(
            ConnectedGmailAccount.id == gmail_account_id,
            ConnectedGmailAccount.org_id == context.organization_id,
        )
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The connected Gmail account was not found.",
        )
    assignment = db.scalar(
        select(UserGmailAssignment.id).where(
            UserGmailAssignment.gmail_account_id == gmail_account_id,
            UserGmailAssignment.user_id == context.user_id,
            UserGmailAssignment.org_id == context.organization_id,
        )
    )
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This Gmail account is not assigned to the current user.",
        )
    return account


def _build_gmail_service(account: ConnectedGmailAccount) -> Any:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not fully configured on the server.",
        )

    credentials = Credentials(
        token=None,
        refresh_token=decrypt_refresh_token(account.encrypted_refresh_token),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        scopes=GMAIL_API_SCOPES,
    )
    return build(
        "gmail",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )


def list_gmail_messages(
    db: Session,
    gmail_account_id: UUID,
    *,
    query: str | None,
    page_token: str | None,
    max_results: int,
) -> GmailMessagesResponse:
    service = get_gmail_service(db, gmail_account_id)
    parameters: dict[str, Any] = {
        "userId": "me",
        "labelIds": ["INBOX"],
        "maxResults": max_results,
    }
    if query:
        parameters["q"] = query
    if page_token:
        parameters["pageToken"] = page_token

    response = _execute_google_request(
        service.users().messages().list(**parameters),
        missing_message_detail="The Gmail inbox could not be loaded.",
    )
    message_items = response.get("messages", [])
    if not isinstance(message_items, list):
        raise _invalid_gmail_response()

    messages = []
    for item in message_items:
        if not isinstance(item, dict):
            raise _invalid_gmail_response()
        message_id = item.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise _invalid_gmail_response()
        metadata = _get_message_metadata(service, message_id)
        messages.append(
            GmailMessageSummary(
                id=message_id,
                thread_id=_required_string(metadata, "threadId"),
                from_address=_message_header(metadata, "From"),
                subject=_message_header(metadata, "Subject") or "(no subject)",
                date=_message_header(metadata, "Date"),
                snippet=_safe_text(item.get("snippet"))[:1000],
                is_unread=(
                    isinstance(item.get("labelIds"), list)
                    and "UNREAD" in item["labelIds"]
                ),
            )
        )

    next_page_token = response.get("nextPageToken")
    if next_page_token is not None and not isinstance(next_page_token, str):
        raise _invalid_gmail_response()
    result_size = response.get("resultSizeEstimate", 0)
    if not isinstance(result_size, int):
        raise _invalid_gmail_response()
    return GmailMessagesResponse(
        messages=messages,
        next_page_token=next_page_token,
        result_size_estimate=result_size,
    )


def get_gmail_message(
    db: Session,
    gmail_account_id: UUID,
    message_id: str,
) -> GmailMessageDetailResponse:
    service = get_gmail_service(db, gmail_account_id)
    message = _execute_google_request(
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full"),
        missing_message_detail="The Gmail message was not found.",
    )
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise _invalid_gmail_response()
    body_text = _extract_message_body(payload)
    labels = message.get("labelIds")
    return GmailMessageDetailResponse(
        id=_required_string(message, "id"),
        thread_id=_required_string(message, "threadId"),
        from_address=_message_header(message, "From"),
        subject=_message_header(message, "Subject") or "(no subject)",
        date=_message_header(message, "Date"),
        snippet=_safe_text(message.get("snippet"))[:1000],
        is_unread=isinstance(labels, list) and "UNREAD" in labels,
        body_text=body_text[:MAX_GMAIL_BODY_CHARACTERS],
    )


def reply_to_gmail_message(
    db: Session,
    gmail_account_id: UUID,
    message_id: str,
    body_text: str,
) -> GmailReplyResponse:
    service = get_gmail_service(db, gmail_account_id)
    original = _get_message_metadata(service, message_id)
    thread_id = _required_string(original, "threadId")
    recipient_header = (
        _message_header(original, "Reply-To")
        or _message_header(original, "From")
    )
    recipients = [
        address
        for _, address in getaddresses([recipient_header])
        if address and "\r" not in address and "\n" not in address
    ]
    if not recipients:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The original message does not have a valid reply address.",
        )

    subject = _message_header(original, "Subject")
    subject = _safe_header(subject or "")
    if subject and re.match(r"^\s*re\s*:", subject, flags=re.IGNORECASE) is None:
        subject = f"Re: {subject}"

    reply = EmailMessage(policy=policy.SMTP)
    reply["To"] = recipients[0]
    reply["From"] = _assigned_account_email(db, gmail_account_id)
    if subject:
        reply["Subject"] = subject
    original_message_id = _safe_header(
        _message_header(original, "Message-ID") or ""
    )
    original_references = _safe_header(
        _message_header(original, "References") or ""
    )
    if original_message_id:
        reply["In-Reply-To"] = original_message_id
        references = " ".join(
            part
            for part in (original_references, original_message_id)
            if part
        )
        reply["References"] = references
    reply.set_content(body_text)
    raw_message = (
        base64.urlsafe_b64encode(reply.as_bytes())
        .decode("ascii")
        .rstrip("=")
    )

    sent = _execute_google_request(
        service.users().messages().send(
            userId="me",
            body={"raw": raw_message, "threadId": thread_id},
        ),
        missing_message_detail="The reply could not be sent.",
    )
    return GmailReplyResponse(
        id=_required_string(sent, "id"),
        thread_id=_required_string(sent, "threadId"),
    )


def _get_message_metadata(service: Any, message_id: str) -> dict[str, Any]:
    return _execute_google_request(
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=list(MESSAGE_METADATA_HEADERS),
        ),
        missing_message_detail="The Gmail message was not found.",
    )


def _assigned_account_email(db: Session, gmail_account_id: UUID) -> str:
    context = get_tenant_context() or db.info.get("tenant_context")
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required to access Gmail.",
        )
    account = db.scalar(
        select(ConnectedGmailAccount).where(
            ConnectedGmailAccount.id == gmail_account_id,
            ConnectedGmailAccount.org_id == context.organization_id,
        )
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The connected Gmail account was not found.",
        )
    return account.email_address


def _execute_google_request(
    request: Any,
    *,
    missing_message_detail: str,
) -> dict[str, Any]:
    try:
        response = request.execute()
    except HttpError as exc:
        response_status = getattr(exc.resp, "status", None)
        if response_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=missing_message_detail,
            ) from exc
        if response_status in (401, 403):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Gmail authorization failed. Reconnect the account and approve the requested permissions.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The Gmail API could not complete the request.",
        ) from exc
    except RefreshError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Gmail authorization expired or was revoked. Reconnect the account.",
        ) from exc

    if not isinstance(response, dict):
        raise _invalid_gmail_response()
    return response


def _message_header(message: dict[str, Any], name: str) -> str:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return ""
    headers = payload.get("headers", [])
    if not isinstance(headers, list):
        return ""
    for header in headers:
        if (
            isinstance(header, dict)
            and isinstance(header.get("name"), str)
            and header["name"].casefold() == name.casefold()
            and isinstance(header.get("value"), str)
        ):
            try:
                return str(make_header(decode_header(header["value"])))
            except (LookupError, UnicodeDecodeError, ValueError):
                return _safe_text(header["value"])
    return ""


def _extract_message_body(payload: dict[str, Any]) -> str:
    plain_text = _collect_mime_text(payload, "text/plain")
    if plain_text:
        return plain_text[:MAX_GMAIL_BODY_CHARACTERS]
    html_text = _collect_mime_text(payload, "text/html")
    if html_text:
        parser = _PlainTextHTMLParser()
        parser.feed(html_text)
        parser.close()
        return parser.text()[:MAX_GMAIL_BODY_CHARACTERS]
    return ""


def _html_to_plain_text(html_content: str) -> str:
    parser = _PlainTextHTMLParser()
    parser.feed(html_content)
    parser.close()
    return parser.text()


def _collect_mime_text(payload: dict[str, Any], target_mime_type: str) -> str:
    chunks: list[str] = []
    pending = [payload]
    character_count = 0
    while pending and character_count < MAX_GMAIL_BODY_CHARACTERS:
        part = pending.pop()
        if not isinstance(part, dict):
            continue
        if part.get("mimeType") == target_mime_type:
            body = part.get("body")
            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, str):
                decoded = _decode_body_data(data)
                remaining = MAX_GMAIL_BODY_CHARACTERS - character_count
                chunks.append(decoded[:remaining])
                character_count += min(len(decoded), remaining)
        children = part.get("parts")
        if isinstance(children, list):
            pending.extend(reversed(children))
    return "\n".join(chunks)[:MAX_GMAIL_BODY_CHARACTERS]


def _decode_body_data(data: str) -> str:
    try:
        data = data[: MAX_GMAIL_BODY_CHARACTERS * 4]
        padding = "=" * (-len(data) % 4)
        raw = base64.urlsafe_b64decode(data + padding)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The Gmail message body could not be decoded.",
        ) from exc
    return raw.decode("utf-8", errors="replace")


class _PlainTextHTMLParser(HTMLParser):
    _BLOCK_TAGS = {
        "address",
        "article",
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "li",
        "p",
        "pre",
        "section",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif tag in self._BLOCK_TAGS and self._ignored_depth == 0:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in self._BLOCK_TAGS and self._ignored_depth == 0:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        lines = (line.strip() for line in "".join(self._chunks).splitlines())
        return "\n".join(line for line in lines if line)


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _invalid_gmail_response()
    return value


def _safe_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _safe_header(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _invalid_gmail_response() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Google returned an invalid Gmail response.",
    )


def _fernet() -> Fernet:
    key = settings.gmail_token_encryption_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail token encryption is not configured on the server.",
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GMAIL_TOKEN_ENCRYPTION_KEY is invalid.",
        ) from exc
