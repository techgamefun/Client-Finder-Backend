import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from app.models import Company, Contact, LeadSource, WebsiteCrawl
from app.services.normalization import normalize_company_name, normalize_domain
from app.services.website_crawler import CrawlData, crawl_website


NAME_STOPWORDS = {
    "and",
    "co",
    "company",
    "corp",
    "corporation",
    "group",
    "inc",
    "limited",
    "llc",
    "ltd",
    "plc",
    "private",
    "pvt",
}
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
}


async def crawl_research_source(
    company_id: UUID,
    source_url: str,
    db: Session,
) -> None:
    crawl = await crawl_website(source_url)
    await run_in_threadpool(
        _persist_research_enrichment,
        company_id,
        source_url,
        crawl,
        db,
    )


def _persist_research_enrichment(
    company_id: UUID,
    source_url: str,
    crawl: CrawlData,
    db: Session,
) -> None:
    company = db.scalar(
        select(Company)
        .options(selectinload(Company.contacts), selectinload(Company.sources))
        .where(Company.id == company_id)
    )
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    source = db.scalar(
        select(LeadSource).where(LeadSource.source_url == source_url)
    )
    if source is not None and source.company_id != company.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This research source is already attached to another company.",
        )

    candidate_website = _company_website_candidate(company.name, crawl)
    if company.website:
        candidate_domain = normalize_domain(company.website)
    elif company.domain:
        candidate_domain = normalize_domain(company.domain)
        if candidate_domain:
            candidate_website = f"https://{candidate_domain}"
    else:
        candidate_domain = normalize_domain(candidate_website)

    if candidate_domain:
        domain_owner = db.scalar(
            select(Company.id)
            .where(Company.domain == candidate_domain)
            .where(Company.id != company.id)
            .limit(1)
        )
        if domain_owner is not None:
            candidate_domain = None
            candidate_website = None

    if not company.website and candidate_website:
        company.website = candidate_website
    if not company.domain and candidate_domain:
        company.domain = candidate_domain

    trusted_domains = {
        domain
        for value in (company.website, company.domain, candidate_website)
        if (domain := normalize_domain(value))
    }
    existing_emails = {
        contact.business_email.casefold()
        for contact in company.contacts
        if contact.business_email
    }
    for email in crawl.emails:
        normalized_email = email.strip().lower()
        email_domain = normalized_email.rpartition("@")[2]
        if email_domain not in trusted_domains:
            continue
        if normalized_email and normalized_email not in existing_emails:
            company.contacts.append(
                Contact(
                    business_email=normalized_email,
                    is_publicly_listed=True,
                )
            )
            existing_emails.add(normalized_email)

    existing_phones = {
        _normalize_phone(contact.business_phone)
        for contact in company.contacts
        if contact.business_phone
    }
    source_host = (urlsplit(crawl.website_url).hostname or "").lower()
    phones_are_company_listed = _host_matches_company(
        source_host,
        company.name,
    )
    for phone in (
        crawl.phones if phones_are_company_listed else []
    ):
        normalized_phone = _normalize_phone(phone)
        if normalized_phone and normalized_phone not in existing_phones:
            company.contacts.append(
                Contact(
                    business_phone=phone,
                    is_publicly_listed=True,
                )
            )
            existing_phones.add(normalized_phone)

    now = datetime.now(timezone.utc)
    evidence = _crawl_evidence(crawl)
    source_domain = normalize_domain(source_url)
    if source_domain is None:
        raise ValueError(
            f"Could not determine source domain for {source_url!r}."
        )
    if source is None:
        source = LeadSource(
            source_url=source_url,
            source_domain=source_domain,
            source_type="company_research",
            evidence=evidence,
            verified_at=now,
        )
        company.sources.append(source)
    else:
        source.source_domain = source_domain
        source.evidence = evidence
        source.verified_at = now

    try:
        _upsert_crawl(crawl, db, now)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The source or company domain conflicts with an existing record.",
        ) from exc
    except SQLAlchemyError:
        db.rollback()
        raise


def _company_website_candidate(
    company_name: str,
    crawl: CrawlData,
) -> str | None:
    source_host = (urlsplit(crawl.website_url).hostname or "").lower()
    if _host_matches_company(source_host, company_name):
        return _homepage_url(crawl.website_url)

    for url, label in crawl.company_links:
        host = (urlsplit(url).hostname or "").lower()
        if host != source_host and _host_matches_company(
            f"{label} {host}",
            company_name,
        ):
            return _homepage_url(url)

    for email in crawl.emails:
        email_domain = email.rpartition("@")[2].lower()
        if (
            email_domain
            and email_domain not in PERSONAL_EMAIL_DOMAINS
            and _host_matches_company(email_domain, company_name)
        ):
            if email_domain != source_host and not email_domain.endswith(
                f".{source_host}"
            ):
                return f"https://{email_domain}"

    return None


def _host_matches_company(host: str, company_name: str) -> bool:
    name_tokens = set(normalize_company_name(company_name).split())
    name_tokens.difference_update(NAME_STOPWORDS)
    registrable_name = host.casefold().rsplit(".", 1)[0]
    host_tokens = set(re.split(r"[^a-z0-9]+", registrable_name))
    host_tokens.discard("")
    return any(token in host_tokens for token in name_tokens)


def _homepage_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


def _normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits if len(digits) >= 7 else None


def _crawl_evidence(crawl: CrawlData) -> str:
    sections = [
        f"{label} page:\n{content}"
        for label, content in (
            ("About", crawl.about_content),
            ("Contact", crawl.contact_content),
            ("Mission and goals", crawl.goals_content),
        )
        if content
    ]
    if crawl.emails:
        sections.append(f"Public email addresses: {', '.join(crawl.emails)}")
    if crawl.phones:
        sections.append(f"Public phone numbers: {', '.join(crawl.phones)}")
    return "\n\n".join(sections)


def _upsert_crawl(
    crawl: CrawlData,
    db: Session,
    crawled_at: datetime,
) -> None:
    record = db.scalar(
        select(WebsiteCrawl)
        .where(WebsiteCrawl.website_url == crawl.website_url)
        .limit(1)
    )
    if record is None:
        record = WebsiteCrawl(website_url=crawl.website_url)
        db.add(record)

    record.about_page_url = crawl.about_page_url
    record.about_content = crawl.about_content
    record.contact_page_url = crawl.contact_page_url
    record.contact_content = crawl.contact_content
    record.goals_page_url = crawl.goals_page_url
    record.goals_content = crawl.goals_content
    record.emails = crawl.emails
    record.social_links = crawl.social_links
    record.crawled_at = crawled_at
