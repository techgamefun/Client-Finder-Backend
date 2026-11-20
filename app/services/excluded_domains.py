from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Table, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import (
    CrawlerDomainFailure,
    ExcludedDomain,
    WebsiteCrawlJob,
)
from app.services.normalization import normalize_domain
from app.tenant_context import (
    TenantContext,
    get_tenant_context,
    reset_tenant_context,
    set_tenant_context,
)


SYSTEM_PRESET_DOMAINS = (
    "google.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
    "reddit.com",
    "wikipedia.org",
    "amazon.com",
    "apple.com",
    "microsoft.com",
)
UNREACHABLE_FAILURE_THRESHOLD = 3
AUTO_RETRYABLE_FAILURE_CODES = {
    "dns_resolution",
    "connection_refused",
    "timeout",
}


def find_excluded_domain(
    db: Session,
    url_or_domain: str,
) -> ExcludedDomain | None:
    hostname = normalize_domain(url_or_domain)
    if hostname is None:
        return None

    labels = hostname.split(".")
    candidate_domains = [
        ".".join(labels[index:])
        for index in range(len(labels) - 1)
    ]
    return db.scalar(
        select(ExcludedDomain)
        .where(ExcludedDomain.domain.in_(candidate_domains))
        .order_by(func.length(ExcludedDomain.domain).desc())
        .limit(1)
    )


def seed_system_excluded_domains(
    db: Session,
    organization_id: UUID,
) -> None:
    current_context = get_tenant_context()
    if (
        current_context is not None
        and current_context.organization_id != organization_id
    ):
        raise RuntimeError(
            "Cannot seed excluded domains for another organization."
        )

    context_token = set_tenant_context(
        TenantContext(
            organization_id=organization_id,
            user_id=current_context.user_id if current_context else None,
            email=current_context.email if current_context else "",
            role=current_context.role if current_context else "system",
        )
    )
    try:
        existing_domains = set(
            db.scalars(
                select(ExcludedDomain.domain).where(
                    ExcludedDomain.organization_id == organization_id
                )
            ).all()
        )
        db.add_all(
            ExcludedDomain(
                organization_id=organization_id,
                domain=domain,
                reason="Big Tech / Social",
                source="system_preset",
            )
            for domain in SYSTEM_PRESET_DOMAINS
            if domain not in existing_domains
        )
        db.flush()
    finally:
        reset_tenant_context(context_token)


def mark_queued_jobs_skipped_for_domain(
    db: Session,
    domain: str,
    reason: str,
) -> int:
    return mark_queued_jobs_skipped_for_domains(db, {domain: reason})


def mark_queued_jobs_skipped_for_domains(
    db: Session,
    exclusions: dict[str, str],
) -> int:
    if not exclusions:
        return 0

    jobs = db.scalars(
        select(WebsiteCrawlJob).where(WebsiteCrawlJob.status == "queued")
    ).all()
    now = datetime.now(timezone.utc)
    skipped_count = 0
    for job in jobs:
        job_domain = normalize_domain(job.website_url)
        if job_domain is None:
            continue
        labels = job_domain.split(".")
        reason = next(
            (
                exclusions[candidate]
                for index in range(len(labels) - 1)
                if (candidate := ".".join(labels[index:])) in exclusions
            ),
            None,
        )
        if reason is None:
            continue
        job.status = "skipped"
        job.skip_reason = reason
        job.error = None
        job.updated_at = now
        job.completed_at = now
        skipped_count += 1
    return skipped_count


def record_unreachable_domain_failure(
    db: Session,
    url_or_domain: str,
    last_error: str,
) -> int:
    hostname = normalize_domain(url_or_domain)
    if hostname is None:
        raise ValueError("A valid domain is required to record crawl failure.")
    context = get_tenant_context() or db.info.get("tenant_context")
    if context is None:
        raise RuntimeError("Tenant context is required to record crawl failure.")

    now = datetime.now(timezone.utc)
    tracker_table = CrawlerDomainFailure.__table__
    excluded_table = ExcludedDomain.__table__
    insert = _dialect_insert(db, tracker_table)
    tracker_statement = insert.values(
        id=uuid4(),
        organization_id=context.organization_id,
        domain=hostname,
        failure_count=1,
        last_error=last_error,
        updated_at=now,
    )
    tracker_statement = tracker_statement.on_conflict_do_update(
        index_elements=[
            tracker_table.c.organization_id,
            tracker_table.c.domain,
        ],
        set_={
            "failure_count": tracker_table.c.failure_count + 1,
            "last_error": last_error,
            "updated_at": now,
        },
    ).returning(tracker_table.c.failure_count)
    failure_count = db.scalar(tracker_statement)
    if failure_count is None:
        raise RuntimeError("Could not update the crawler failure count.")

    if failure_count >= UNREACHABLE_FAILURE_THRESHOLD:
        exclusion_insert = _dialect_insert(db, excluded_table)
        exclusion_statement = exclusion_insert.values(
            id=uuid4(),
            organization_id=context.organization_id,
            domain=hostname,
            reason="Unreachable",
            note=last_error,
            source="auto_crawler",
            failure_count=failure_count,
            created_by_user_id=context.user_id,
            created_at=now,
            updated_at=now,
        )
        exclusion_statement = exclusion_statement.on_conflict_do_update(
            index_elements=[
                excluded_table.c.organization_id,
                excluded_table.c.domain,
            ],
            set_={
                "reason": "Unreachable",
                "note": last_error,
                "source": "auto_crawler",
                "failure_count": failure_count,
                "updated_at": now,
            },
            where=excluded_table.c.source == "auto_crawler",
        )
        db.execute(exclusion_statement)
        mark_queued_jobs_skipped_for_domain(
            db,
            hostname,
            "Unreachable",
        )

    db.commit()
    return failure_count


def _dialect_insert(db: Session, table: Table) -> Any:
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        return postgresql_insert(table)
    if dialect_name == "sqlite":
        return sqlite_insert(table)
    raise RuntimeError(
        f"Excluded-domain upserts are not supported for {dialect_name}."
    )
