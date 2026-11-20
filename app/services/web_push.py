import json
import logging
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PushSubscription, UserPushPreferences


logger = logging.getLogger(__name__)

PushCategory = Literal["website_crawl", "pagespeed", "outreach"]
_PREFERENCE_FIELDS = {
    "website_crawl": "website_crawl_enabled",
    "pagespeed": "pagespeed_enabled",
    "outreach": "outreach_enabled",
}
_MAX_PAYLOAD_BYTES = 3800


class PushNotificationsNotConfiguredError(RuntimeError):
    pass


@dataclass(frozen=True)
class PushDeliverySummary:
    attempted: int
    sent: int
    expired_removed: int
    failed: int
    failure_status_codes: tuple[int | None, ...]


def send_push_to_user(
    *,
    organization_id: UUID,
    user_id: UUID,
    category: PushCategory,
    payload: dict[str, Any],
    db: Session,
) -> PushDeliverySummary:
    if not settings.web_push_configured:
        raise PushNotificationsNotConfiguredError(
            "Web Push is not configured on this server."
        )
    preference_field = _PREFERENCE_FIELDS.get(category)
    if preference_field is None:
        raise ValueError(f"Unsupported push notification category: {category}")

    preferences = db.scalar(
        select(UserPushPreferences).where(
            UserPushPreferences.organization_id == organization_id,
            UserPushPreferences.user_id == user_id,
        )
    )
    if preferences is not None and not getattr(preferences, preference_field):
        return PushDeliverySummary(0, 0, 0, 0, ())

    encoded_payload = json.dumps(payload, separators=(",", ":"))
    if len(encoded_payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError("Push notification payload exceeds the size limit.")

    subscriptions = db.scalars(
        select(PushSubscription).where(
            PushSubscription.organization_id == organization_id,
            PushSubscription.user_id == user_id,
        )
    ).all()
    sent = 0
    expired_removed = 0
    failure_status_codes: list[int | None] = []

    for subscription in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {
                        "p256dh": subscription.p256dh,
                        "auth": subscription.auth,
                    },
                },
                data=encoded_payload,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
                ttl=3600,
                timeout=10,
            )
        except WebPushException as exc:
            response = exc.response
            status_code = response.status_code if response is not None else None
            if status_code in (404, 410):
                db.delete(subscription)
                expired_removed += 1
                continue
            logger.warning(
                "Push delivery failed for subscription %s with status %s.",
                subscription.id,
                status_code,
            )
            failure_status_codes.append(status_code)
        else:
            sent += 1

    if expired_removed:
        db.commit()

    attempted = len(subscriptions)
    return PushDeliverySummary(
        attempted=attempted,
        sent=sent,
        expired_removed=expired_removed,
        failed=len(failure_status_codes),
        failure_status_codes=tuple(failure_status_codes),
    )
