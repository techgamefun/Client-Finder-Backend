from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy import and_, case, exists, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.models import (
    Company,
    ConnectedGmailAccount,
    Contact,
    EmailCampaign,
    EmailCampaignRecipient,
    PageSpeedScanJob,
    SearchKeyword,
    UserGmailAssignment,
    UserProviderCredential,
    Website,
    WebsiteCrawlJob,
    website_search_keywords,
)
from app.schemas.overview import (
    OverviewAttentionItem,
    OverviewCoverage,
    OverviewCoverageRate,
    OverviewFunnel,
    OverviewFunnelStage,
    OverviewJobs,
    OverviewKpi,
    OverviewKpis,
    OverviewOnboarding,
    OverviewOnboardingStep,
    OverviewPeriod,
    OverviewQueueHealth,
    OverviewRecentLead,
    OverviewRange,
    OverviewResponse,
    OverviewSeriesPoint,
    OverviewStatus,
)
from app.tenant_context import TenantContext


RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}
SPARKLINE_DAYS = 30


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _count_range(
    model: Any,
    organization_id: Any,
    time_column: Any,
    start: datetime,
    end: datetime,
    *conditions: Any,
) -> Any:
    return (
        select(func.count(model.id))
        .where(
            model.organization_id == organization_id
            if hasattr(model, "organization_id")
            else model.org_id == organization_id,
            time_column >= start,
            time_column < end,
            *conditions,
        )
        .scalar_subquery()
    )


def _count_all(
    model: Any,
    organization_id: Any,
    *conditions: Any,
) -> Any:
    return (
        select(func.count(model.id))
        .where(
            model.organization_id == organization_id
            if hasattr(model, "organization_id")
            else model.org_id == organization_id,
            *conditions,
        )
        .scalar_subquery()
    )


def _metric(
    current: int,
    previous: int,
    sparkline: list[int],
    *,
    denominator: int | None = None,
) -> OverviewKpi:
    delta = current - previous
    return OverviewKpi(
        current=current,
        previous=previous,
        absolute_change=delta,
        delta_percent=round(delta / previous * 100, 1) if previous else None,
        sparkline=sparkline,
        denominator=denominator,
        percentage=(
            round(current / denominator * 100, 1)
            if denominator
            else None
        ),
    )


def _rounded_percentage(count: int, total: int) -> float | None:
    return round(count / total * 100, 1) if total else None


def build_overview(
    db: Session,
    context: TenantContext,
    range_name: OverviewRange,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> OverviewResponse:
    if range_name not in RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="range must be one of 7d, 30d, or 90d.",
        )
    try:
        user_timezone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tz must be a valid IANA timezone.",
        ) from exc

    current_end = _as_utc(now or _utc_now())
    range_duration = timedelta(days=RANGE_DAYS[range_name])
    current_start = current_end - range_duration
    previous_start = current_start - range_duration
    organization_id = context.organization_id

    has_contact = exists(
        select(Contact.id).where(
            Contact.company_id == Company.id,
            Contact.organization_id == organization_id,
        )
    )
    has_reachable_contact = exists(
        select(Contact.id).where(
            Contact.company_id == Company.id,
            Contact.organization_id == organization_id,
            or_(
                and_(
                    Contact.business_email.is_not(None),
                    func.trim(Contact.business_email) != "",
                ),
                and_(
                    Contact.business_phone.is_not(None),
                    func.trim(Contact.business_phone) != "",
                ),
            ),
        )
    )
    sent_recipient = and_(
        EmailCampaignRecipient.status == "sent",
        EmailCampaignRecipient.sent_at.is_not(None),
    )
    has_email = exists(
        select(Contact.id).where(
            Contact.company_id == Company.id,
            Contact.organization_id == organization_id,
            Contact.business_email.is_not(None),
            func.trim(Contact.business_email) != "",
        )
    )
    has_phone = exists(
        select(Contact.id).where(
            Contact.company_id == Company.id,
            Contact.organization_id == organization_id,
            Contact.business_phone.is_not(None),
            func.trim(Contact.business_phone) != "",
        )
    )
    has_website = or_(
        and_(
            Company.website.is_not(None),
            func.trim(Company.website) != "",
        ),
        and_(
            Company.domain.is_not(None),
            func.trim(Company.domain) != "",
        ),
    )
    has_industry = and_(
        Company.industry.is_not(None),
        func.trim(Company.industry) != "",
    )

    kpi_row = db.execute(
        select(
            _count_range(
                Website,
                organization_id,
                Website.created_at,
                current_start,
                current_end,
            ).label("websites_current"),
            _count_range(
                Website,
                organization_id,
                Website.created_at,
                previous_start,
                current_start,
            ).label("websites_previous"),
            _count_range(
                Company,
                organization_id,
                Company.created_at,
                current_start,
                current_end,
                has_contact,
            ).label("leads_current"),
            _count_range(
                Company,
                organization_id,
                Company.created_at,
                previous_start,
                current_start,
                has_contact,
            ).label("leads_previous"),
            _count_range(
                Company,
                organization_id,
                Company.created_at,
                current_start,
                current_end,
                has_reachable_contact,
            ).label("reachable_current"),
            _count_range(
                Company,
                organization_id,
                Company.created_at,
                previous_start,
                current_start,
                has_reachable_contact,
            ).label("reachable_previous"),
            _count_range(
                EmailCampaignRecipient,
                organization_id,
                EmailCampaignRecipient.sent_at,
                current_start,
                current_end,
                sent_recipient,
            ).label("emails_current"),
            _count_range(
                EmailCampaignRecipient,
                organization_id,
                EmailCampaignRecipient.sent_at,
                previous_start,
                current_start,
                sent_recipient,
            ).label("emails_previous"),
            _count_range(
                Company,
                organization_id,
                Company.created_at,
                current_start,
                current_end,
            ).label("companies_current"),
            _count_all(Company, organization_id).label("companies_total"),
            _count_all(Website, organization_id).label("websites_total"),
            select(
                func.coalesce(
                    func.sum(case((has_email, 1), else_=0)),
                    0,
                )
            )
            .select_from(Company)
            .where(Company.organization_id == organization_id)
            .scalar_subquery()
            .label("with_email"),
            select(
                func.coalesce(
                    func.sum(case((has_phone, 1), else_=0)),
                    0,
                )
            )
            .select_from(Company)
            .where(Company.organization_id == organization_id)
            .scalar_subquery()
            .label("with_phone"),
            select(
                func.coalesce(
                    func.sum(case((has_website, 1), else_=0)),
                    0,
                )
            )
            .select_from(Company)
            .where(Company.organization_id == organization_id)
            .scalar_subquery()
            .label("with_website"),
            select(
                func.coalesce(
                    func.sum(case((has_industry, 1), else_=0)),
                    0,
                )
            )
            .select_from(Company)
            .where(Company.organization_id == organization_id)
            .scalar_subquery()
            .label("with_industry"),
            _count_range(
                WebsiteCrawlJob,
                organization_id,
                WebsiteCrawlJob.completed_at,
                current_start,
                current_end,
                WebsiteCrawlJob.status == "succeeded",
            ).label("crawls_succeeded"),
            _count_range(
                WebsiteCrawlJob,
                organization_id,
                WebsiteCrawlJob.completed_at,
                current_start,
                current_end,
                WebsiteCrawlJob.status == "failed",
            ).label("crawls_failed"),
        )
    ).one()

    sparkline_start_local = (
        current_end.astimezone(user_timezone).date()
        - timedelta(days=SPARKLINE_DAYS - 1)
    )
    sparkline_start = datetime.combine(
        sparkline_start_local,
        datetime.min.time(),
        tzinfo=user_timezone,
    ).astimezone(timezone.utc)
    chart_start = min(current_start, sparkline_start)

    event_rows = union_all(
        select(
            literal("websites").label("metric"),
            Website.created_at.label("occurred_at"),
        ).where(
            Website.organization_id == organization_id,
            Website.created_at >= chart_start,
            Website.created_at < current_end,
        ),
        select(
            literal("leads").label("metric"),
            Company.created_at.label("occurred_at"),
        ).where(
            Company.organization_id == organization_id,
            Company.created_at >= chart_start,
            Company.created_at < current_end,
            has_contact,
        ),
        select(
            literal("reachable").label("metric"),
            Company.created_at.label("occurred_at"),
        ).where(
            Company.organization_id == organization_id,
            Company.created_at >= chart_start,
            Company.created_at < current_end,
            has_reachable_contact,
        ),
        select(
            literal("emails").label("metric"),
            EmailCampaignRecipient.sent_at.label("occurred_at"),
        ).where(
            EmailCampaignRecipient.org_id == organization_id,
            EmailCampaignRecipient.sent_at >= chart_start,
            EmailCampaignRecipient.sent_at < current_end,
            sent_recipient,
        ),
    ).subquery()
    event_rows_result = db.execute(
        select(event_rows.c.metric, event_rows.c.occurred_at)
    ).all()

    current_start_date = current_start.astimezone(user_timezone).date()
    current_end_date = current_end.astimezone(user_timezone).date()
    series_dates = _dates_between(current_start_date, current_end_date)
    sparkline_dates = _dates_between(
        sparkline_start_local,
        current_end_date,
    )
    series_counts = {
        "websites": {day: 0 for day in series_dates},
        "leads": {day: 0 for day in series_dates},
    }
    sparkline_counts = {
        name: {day: 0 for day in sparkline_dates}
        for name in ("websites", "leads", "reachable", "emails")
    }
    for metric_name, occurred_at in event_rows_result:
        event_date = _as_utc(occurred_at).astimezone(user_timezone).date()
        if metric_name in series_counts and event_date in series_counts[metric_name]:
            series_counts[metric_name][event_date] += 1
        if event_date in sparkline_counts[metric_name]:
            sparkline_counts[metric_name][event_date] += 1

    series = [
        OverviewSeriesPoint(
            date=day.isoformat(),
            websites_discovered=series_counts["websites"][day],
            leads_created=series_counts["leads"][day],
        )
        for day in series_dates
    ]

    website_has_contact = exists(
        select(Contact.id)
        .join(Company, Company.id == Contact.company_id)
        .where(
            func.lower(Company.domain) == func.lower(Website.domain),
            Company.organization_id == organization_id,
            Contact.organization_id == organization_id,
        )
    )
    website_has_sent_campaign = exists(
        select(EmailCampaignRecipient.id)
        .join(Company, Company.id == EmailCampaignRecipient.company_id)
        .where(
            func.lower(Company.domain) == func.lower(Website.domain),
            Company.organization_id == organization_id,
            EmailCampaignRecipient.org_id == organization_id,
            EmailCampaignRecipient.status == "sent",
        )
    )
    funnel_row = db.execute(
        select(
            func.count(Website.id).label("discovered"),
            func.coalesce(
                func.sum(case((Website.crawl_id.is_not(None), 1), else_=0)),
                0,
            ).label("crawled"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                Website.crawl_id.is_not(None),
                                Website.status == "lead",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("added"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                Website.crawl_id.is_not(None),
                                Website.status == "lead",
                                website_has_contact,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("has_contact"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                Website.crawl_id.is_not(None),
                                Website.status == "lead",
                                website_has_contact,
                                website_has_sent_campaign,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("emailed"),
        ).where(
            Website.organization_id == organization_id,
            Website.created_at >= current_start,
            Website.created_at < current_end,
        )
    ).one()
    funnel_counts = [
        int(funnel_row.discovered or 0),
        int(funnel_row.crawled or 0),
        int(funnel_row.added or 0),
        int(funnel_row.has_contact or 0),
        int(funnel_row.emailed or 0),
    ]
    funnel_labels = [
        ("discovered", "Discovered"),
        ("crawled", "Crawled"),
        ("added", "Added to leads"),
        ("has_contact", "Has contact"),
        ("emailed", "Emailed"),
    ]
    funnel_stages: list[OverviewFunnelStage] = []
    largest_drop: tuple[float, str] | None = None
    previous_count: int | None = None
    for (key, label), count in zip(funnel_labels, funnel_counts, strict=True):
        conversion = (
            _rounded_percentage(count, previous_count)
            if previous_count is not None
            else None
        )
        funnel_stages.append(
            OverviewFunnelStage(
                key=key,
                label=label,
                count=count,
                conversion_percent=conversion,
            )
        )
        if previous_count:
            drop_percent = 100 - (count / previous_count * 100)
            if largest_drop is None or drop_percent > largest_drop[0]:
                largest_drop = (
                    drop_percent,
                    f"Biggest drop: {round(drop_percent)}% of {funnel_labels[len(funnel_stages) - 2][1].lower()} "
                    f"have not reached {label.lower()}.",
                )
        previous_count = count
    funnel = OverviewFunnel(
        stages=funnel_stages,
        biggest_drop=largest_drop[1] if largest_drop is not None else None,
    )

    status_rows = db.execute(
        select(Company.lead_status, func.count(Company.id))
        .where(
            Company.organization_id == organization_id,
            has_contact,
        )
        .group_by(Company.lead_status)
        .order_by(func.count(Company.id).desc(), Company.lead_status)
    ).all()
    lead_total = sum(int(count) for _, count in status_rows)
    status_breakdown = [
        OverviewStatus(
            status=lead_status,
            count=int(count),
            percentage=_rounded_percentage(int(count), lead_total),
        )
        for lead_status, count in status_rows
    ]

    successful_crawls = int(kpi_row.crawls_succeeded or 0)
    completed_crawls = successful_crawls + int(kpi_row.crawls_failed or 0)
    companies_total = int(kpi_row.companies_total or 0)
    coverage = OverviewCoverage(
        companies_total=companies_total,
        with_email=_coverage_rate(
            int(kpi_row.with_email or 0),
            companies_total,
        ),
        with_phone=_coverage_rate(
            int(kpi_row.with_phone or 0),
            companies_total,
        ),
        with_website=_coverage_rate(
            int(kpi_row.with_website or 0),
            companies_total,
        ),
        with_industry=_coverage_rate(
            int(kpi_row.with_industry or 0),
            companies_total,
        ),
        crawl_success=_coverage_rate(successful_crawls, completed_crawls),
    )

    jobs_since = current_end - timedelta(hours=24)
    latest_crawler_jobs = (
        select(WebsiteCrawlJob.status)
        .where(WebsiteCrawlJob.organization_id == organization_id)
        .order_by(WebsiteCrawlJob.created_at.desc(), WebsiteCrawlJob.id.desc())
        .limit(100)
        .subquery()
    )
    latest_pagespeed_jobs = (
        select(PageSpeedScanJob.status)
        .where(PageSpeedScanJob.organization_id == organization_id)
        .order_by(PageSpeedScanJob.created_at.desc(), PageSpeedScanJob.id.desc())
        .limit(100)
        .subquery()
    )
    jobs_row = db.execute(
        select(
            _count_range(
                WebsiteCrawlJob,
                organization_id,
                WebsiteCrawlJob.created_at,
                jobs_since,
                current_end,
                WebsiteCrawlJob.status == "queued",
            ).label("crawler_queued"),
            _count_range(
                WebsiteCrawlJob,
                organization_id,
                WebsiteCrawlJob.created_at,
                jobs_since,
                current_end,
                WebsiteCrawlJob.status == "running",
            ).label("crawler_running"),
            _count_range(
                WebsiteCrawlJob,
                organization_id,
                WebsiteCrawlJob.created_at,
                jobs_since,
                current_end,
                WebsiteCrawlJob.status == "failed",
            ).label("crawler_failed"),
            _count_range(
                PageSpeedScanJob,
                organization_id,
                PageSpeedScanJob.created_at,
                jobs_since,
                current_end,
                PageSpeedScanJob.status == "queued",
            ).label("pagespeed_queued"),
            _count_range(
                PageSpeedScanJob,
                organization_id,
                PageSpeedScanJob.created_at,
                jobs_since,
                current_end,
                PageSpeedScanJob.status == "running",
            ).label("pagespeed_running"),
            _count_range(
                PageSpeedScanJob,
                organization_id,
                PageSpeedScanJob.created_at,
                jobs_since,
                current_end,
                PageSpeedScanJob.status == "failed",
            ).label("pagespeed_failed"),
            select(func.count())
            .select_from(latest_crawler_jobs)
            .where(latest_crawler_jobs.c.status == "failed")
            .scalar_subquery()
            .label("crawler_failed_badge"),
            select(func.count())
            .select_from(latest_pagespeed_jobs)
            .where(latest_pagespeed_jobs.c.status == "failed")
            .scalar_subquery()
            .label("pagespeed_failed_badge"),
        )
    ).one()
    failed_badge_count = int(jobs_row.crawler_failed_badge or 0) + int(
        jobs_row.pagespeed_failed_badge or 0
    )
    jobs = OverviewJobs(
        crawler=OverviewQueueHealth(
            queued=int(jobs_row.crawler_queued or 0),
            running=int(jobs_row.crawler_running or 0),
            failed=int(jobs_row.crawler_failed or 0),
        ),
        pagespeed=OverviewQueueHealth(
            queued=int(jobs_row.pagespeed_queued or 0),
            running=int(jobs_row.pagespeed_running or 0),
            failed=int(jobs_row.pagespeed_failed or 0),
        ),
        failed_badge_count=failed_badge_count,
    )

    has_detail_contact = exists(
        select(Contact.id).where(
            Contact.company_id == Company.id,
            Contact.organization_id == organization_id,
            or_(
                and_(
                    Contact.business_email.is_not(None),
                    func.trim(Contact.business_email) != "",
                ),
                and_(
                    Contact.business_phone.is_not(None),
                    func.trim(Contact.business_phone) != "",
                ),
            ),
        )
    )
    has_assigned_gmail = exists(
        select(UserGmailAssignment.id)
        .join(
            ConnectedGmailAccount,
            ConnectedGmailAccount.id == UserGmailAssignment.gmail_account_id,
        )
        .where(
            UserGmailAssignment.org_id == organization_id,
            ConnectedGmailAccount.org_id == organization_id,
            UserGmailAssignment.user_id == context.user_id,
        )
    )
    has_search = exists(
        select(SearchKeyword.id).where(
            SearchKeyword.organization_id == organization_id
        )
    )
    has_tavily_key = exists(
        select(UserProviderCredential.id).where(
            UserProviderCredential.user_id == context.user_id,
            UserProviderCredential.provider == "tavily",
        )
    )
    has_sent_campaign = exists(
        select(EmailCampaignRecipient.id).where(
            EmailCampaignRecipient.org_id == organization_id,
            EmailCampaignRecipient.status == "sent",
        )
    )
    attention_row = db.execute(
        select(
            _count_all(
                Website,
                organization_id,
                Website.crawl_id.is_not(None),
                Website.status != "lead",
                or_(
                    Website.page_type.is_(None),
                    Website.page_type != "business_listing",
                ),
            ).label("unadded_crawled_websites"),
            _count_all(
                Company,
                organization_id,
                has_contact,
                ~has_detail_contact,
            ).label("leads_without_details"),
            _count_all(
                EmailCampaign,
                organization_id,
                EmailCampaign.status == "paused",
            ).label("paused_campaigns"),
            _count_all(
                EmailCampaignRecipient,
                organization_id,
                EmailCampaignRecipient.status == "failed",
            ).label("failed_campaign_sends"),
            has_assigned_gmail.label("has_assigned_gmail"),
            has_search.label("has_search"),
            has_tavily_key.label("has_tavily_key"),
            has_sent_campaign.label("has_sent_campaign"),
        )
    ).one()
    unadded_crawled_websites = int(
        attention_row.unadded_crawled_websites or 0
    )
    leads_without_details = int(attention_row.leads_without_details or 0)
    paused_campaigns = int(attention_row.paused_campaigns or 0)
    failed_campaign_sends = int(attention_row.failed_campaign_sends or 0)
    attention_candidates = [
        (
            0,
            failed_badge_count,
            "failed_jobs",
            f"{failed_badge_count} failed jobs are in the latest 100 from each queue.",
            "/jobs?status=failed",
            "failed_jobs_badge",
        ),
        (
            1,
            failed_campaign_sends,
            "failed_campaign_sends",
            f"{failed_campaign_sends} campaign sends failed.",
            "/campaigns?status=failed-sends",
            "failed_campaign_sends",
        ),
        (
            2,
            paused_campaigns,
            "paused_campaigns",
            f"{paused_campaigns} campaigns are paused.",
            "/campaigns?status=paused",
            "paused_campaigns",
        ),
        (
            3,
            unadded_crawled_websites,
            "crawled_websites_not_added",
            f"{unadded_crawled_websites} crawled websites have not been added to leads.",
            "/websites?attention=not-added",
            "crawled_websites_not_added",
        ),
        (
            4,
            0 if attention_row.has_assigned_gmail else 1,
            "gmail_not_assigned",
            "Connect and assign a Gmail account to send campaigns.",
            "/settings",
            "gmail_connection",
        ),
        (
            5,
            leads_without_details,
            "leads_without_contact_details",
            f"{leads_without_details} leads have no public email or phone.",
            "/leads?contact_details=missing",
            "leads_without_contact_details",
        ),
    ]
    attention = [
        OverviewAttentionItem(
            id=item_id,
            count=count,
            message=message,
            href=href,
            metric_key=metric_key,
        )
        for _, count, item_id, message, href, metric_key in sorted(
            attention_candidates,
            key=lambda item: item[0],
        )
        if count > 0
    ][:4]
    onboarding = _build_onboarding(
        websites_total=int(kpi_row.websites_total or 0),
        companies_total=companies_total,
        has_search=bool(attention_row.has_search),
        has_tavily_key=bool(attention_row.has_tavily_key),
        has_assigned_gmail=bool(attention_row.has_assigned_gmail),
        has_sent_campaign=bool(attention_row.has_sent_campaign),
    )
    if onboarding is not None:
        attention = []

    contact_count = (
        select(func.count(Contact.id))
        .where(
            Contact.company_id == Company.id,
            Contact.organization_id == organization_id,
        )
        .correlate(Company)
        .scalar_subquery()
    )
    source_keyword = (
        select(SearchKeyword.keyword)
        .select_from(Website)
        .join(
            website_search_keywords,
            website_search_keywords.c.website_id == Website.id,
        )
        .join(
            SearchKeyword,
            SearchKeyword.id
            == website_search_keywords.c.search_keyword_id,
        )
        .where(
            Website.organization_id == organization_id,
            SearchKeyword.organization_id == organization_id,
            Company.domain.is_not(None),
            func.lower(Website.domain) == func.lower(Company.domain),
        )
        .order_by(SearchKeyword.searched_at.desc(), SearchKeyword.id.desc())
        .limit(1)
        .correlate(Company)
        .scalar_subquery()
    )
    recent_rows = db.execute(
        select(
            Company.id,
            Company.name,
            Company.domain,
            Company.lead_status,
            Company.created_at,
            Company.last_email_sent_at,
            contact_count.label("contact_count"),
            source_keyword.label("source_keyword"),
        )
        .where(Company.organization_id == organization_id, has_contact)
        .order_by(Company.created_at.desc(), Company.id.desc())
        .limit(5)
    ).all()
    recent_leads = [
        OverviewRecentLead(
            id=str(row.id),
            name=row.name,
            domain=row.domain,
            contact_count=int(row.contact_count or 0),
            source_keyword=row.source_keyword,
            lead_status=row.lead_status,
            created_at=row.created_at,
            last_email_sent_at=row.last_email_sent_at,
        )
        for row in recent_rows
    ]

    return OverviewResponse(
        range=range_name,
        previous_range=OverviewPeriod(start=previous_start, end=current_start),
        kpis=OverviewKpis(
            websites_discovered=_metric(
                int(kpi_row.websites_current or 0),
                int(kpi_row.websites_previous or 0),
                _counts_for(sparkline_counts, "websites"),
            ),
            leads_created=_metric(
                int(kpi_row.leads_current or 0),
                int(kpi_row.leads_previous or 0),
                _counts_for(sparkline_counts, "leads"),
            ),
            reachable_leads=_metric(
                int(kpi_row.reachable_current or 0),
                int(kpi_row.reachable_previous or 0),
                _counts_for(sparkline_counts, "reachable"),
                denominator=int(kpi_row.companies_current or 0),
            ),
            emails_sent=_metric(
                int(kpi_row.emails_current or 0),
                int(kpi_row.emails_previous or 0),
                _counts_for(sparkline_counts, "emails"),
            ),
        ),
        funnel=funnel,
        series=series,
        status_breakdown=status_breakdown,
        coverage=coverage,
        jobs=jobs,
        attention=attention,
        onboarding=onboarding,
        recent_leads=recent_leads,
    )


def _dates_between(start: date, end: date) -> list[date]:
    return [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    ]


def _counts_for(
    counts: dict[str, dict[date, int]],
    metric_name: str,
) -> list[int]:
    return list(counts[metric_name].values())


def _coverage_rate(count: int, total: int) -> OverviewCoverageRate:
    return OverviewCoverageRate(
        count=count,
        total=total,
        percentage=_rounded_percentage(count, total),
    )


def _build_onboarding(
    *,
    websites_total: int,
    companies_total: int,
    has_search: bool,
    has_tavily_key: bool,
    has_assigned_gmail: bool,
    has_sent_campaign: bool,
) -> OverviewOnboarding | None:
    steps = [
        OverviewOnboardingStep(
            id="connect_tavily",
            label="Connect a Tavily key",
            href="/settings",
            completed=has_tavily_key,
        ),
        OverviewOnboardingStep(
            id="run_search",
            label="Run your first search",
            href="/discovery",
            completed=has_search,
        ),
        OverviewOnboardingStep(
            id="save_websites",
            label="Save websites",
            href="/discovery",
            completed=websites_total > 0,
        ),
        OverviewOnboardingStep(
            id="add_leads",
            label="Add a company to Leads",
            href="/websites",
            completed=companies_total > 0,
        ),
        OverviewOnboardingStep(
            id="connect_gmail",
            label="Connect and assign Gmail",
            href="/settings",
            completed=has_assigned_gmail,
        ),
        OverviewOnboardingStep(
            id="send_campaign",
            label="Send your first campaign",
            href="/campaigns?create=1",
            completed=has_sent_campaign,
        ),
    ]
    return (
        OverviewOnboarding(steps=steps)
        if not all(step.completed for step in steps)
        else None
    )

