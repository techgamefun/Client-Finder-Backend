from datetime import datetime, timezone
from statistics import median
from typing import Any

from sqlalchemy import func, literal, select
from sqlalchemy.orm import Session

from app.models import (
    PageSpeedScan,
    SearchKeyword,
    Website,
    WebsiteCorpusAnalysis,
    WebsiteCrawl,
    WebsiteHypeStatProfile,
    website_search_keywords,
)
from app.schemas.website_analytics import (
    WebsiteAnalyticsBreakdown,
    WebsiteAnalyticsCoverage,
    WebsiteContentKeywordAnalytics,
    WebsiteAnalyticsDomainRating,
    WebsiteAnalyticsPageSpeedDevice,
    WebsiteDiscoveryKeyword,
    WebsiteDiscoveryKeywordAnalytics,
    WebsiteAnalyticsResponse,
    WebsiteKeywordTerm,
)
from app.tenant_context import TenantContext


PAGE_SPEED_CATEGORIES = (
    "performance",
    "accessibility",
    "best-practices",
    "seo",
)
DOMAIN_RATING_BANDS = (
    ("0-19", 0, 20),
    ("20-39", 20, 40),
    ("40-59", 40, 60),
    ("60-79", 60, 80),
    ("80-100", 80, 101),
)
CORPUS_ANALYSIS_VERSION = 2
TOP_KEYWORD_COUNT = 10


def build_website_analytics(
    db: Session,
    context: TenantContext,
) -> WebsiteAnalyticsResponse:
    organization_id = context.organization_id
    website_scope = Website.organization_id == organization_id
    total_websites = int(
        db.scalar(
            select(func.count(Website.id)).where(website_scope),
        )
        or 0
    )
    crawled_websites = int(
        db.scalar(
            select(func.count(Website.id)).where(
                website_scope,
                Website.crawl_id.is_not(None),
            ),
        )
        or 0
    )

    def breakdown(column: Any) -> list[WebsiteAnalyticsBreakdown]:
        rows = db.execute(
            select(
                func.coalesce(column, literal("unclassified")).label("key"),
                func.count(Website.id).label("count"),
            )
            .where(website_scope)
            .group_by("key")
            .order_by(func.count(Website.id).desc(), "key")
        ).all()
        return [
            WebsiteAnalyticsBreakdown(
                key=str(row.key),
                count=int(row.count),
                percentage=_percentage(int(row.count), total_websites),
            )
            for row in rows
        ]

    discovery_source = func.coalesce(
        Website.discovery_provider,
        Website.discovery_source,
    )
    rating_values = [
        float(value)
        for value in db.scalars(
            select(Website.ahrefs_domain_rating).where(
                website_scope,
                Website.ahrefs_domain_rating.is_not(None),
            ),
        )
        if value is not None
    ]
    rating_distribution = [
        WebsiteAnalyticsBreakdown(
            key=label,
            count=sum(lower <= value < upper for value in rating_values),
            percentage=_percentage(
                sum(lower <= value < upper for value in rating_values),
                len(rating_values),
            ),
        )
        for label, lower, upper in DOMAIN_RATING_BANDS
    ]

    latest_scans = (
        select(
            PageSpeedScan.website_id.label("website_id"),
            PageSpeedScan.strategy.label("strategy"),
            PageSpeedScan.category_scores.label("category_scores"),
            func.row_number()
            .over(
                partition_by=(
                    PageSpeedScan.website_id,
                    PageSpeedScan.strategy,
                ),
                order_by=(
                    PageSpeedScan.scanned_at.desc(),
                    PageSpeedScan.id.desc(),
                ),
            )
            .label("row_number"),
        )
        .join(
            Website,
            (Website.id == PageSpeedScan.website_id)
            & (Website.organization_id == PageSpeedScan.organization_id),
        )
        .where(
            Website.organization_id == organization_id,
            PageSpeedScan.website_id.is_not(None),
        )
        .subquery()
    )
    latest_scan_rows = db.execute(
        select(
            latest_scans.c.website_id,
            latest_scans.c.strategy,
            latest_scans.c.category_scores,
        ).where(latest_scans.c.row_number == 1),
    ).all()
    pagespeed_by_strategy = {
        "desktop": _pagespeed_summary(
            row.category_scores
            for row in latest_scan_rows
            if row.strategy == "desktop"
        ),
        "mobile": _pagespeed_summary(
            row.category_scores
            for row in latest_scan_rows
            if row.strategy == "mobile"
        ),
    }

    hypestat_count = int(
        db.scalar(
            select(func.count(WebsiteHypeStatProfile.website_id)).where(
                WebsiteHypeStatProfile.organization_id == organization_id,
                WebsiteHypeStatProfile.status == "succeeded",
                WebsiteHypeStatProfile.payload.is_not(None),
            ),
        )
        or 0
    )
    pagespeed_any_count = len(
        {row.website_id for row in latest_scan_rows},
    )
    desktop_count = pagespeed_by_strategy["desktop"].scanned_websites
    mobile_count = pagespeed_by_strategy["mobile"].scanned_websites
    content_keywords = _content_keyword_analytics(
        db,
        organization_id=organization_id,
        crawled_websites=crawled_websites,
    )
    discovery_keywords = _discovery_keyword_analytics(
        db,
        organization_id=organization_id,
        total_websites=total_websites,
    )
    return WebsiteAnalyticsResponse(
        generated_at=datetime.now(timezone.utc),
        total_websites=total_websites,
        crawled_websites=crawled_websites,
        crawled_percentage=_percentage(crawled_websites, total_websites)
        if total_websites
        else None,
        coverage=WebsiteAnalyticsCoverage(
            domain_rating=len(rating_values),
            hypestat=hypestat_count,
            pagespeed_any=pagespeed_any_count,
            pagespeed_desktop=desktop_count,
            pagespeed_mobile=mobile_count,
        ),
        status_breakdown=breakdown(Website.status),
        discovery_source_breakdown=breakdown(discovery_source),
        site_type_breakdown=breakdown(Website.category),
        page_type_breakdown=breakdown(Website.page_type),
        domain_rating=WebsiteAnalyticsDomainRating(
            count=len(rating_values),
            average=_rounded(sum(rating_values) / len(rating_values))
            if rating_values
            else None,
            median=_rounded(median(rating_values)) if rating_values else None,
            distribution=rating_distribution,
        ),
        pagespeed_desktop=pagespeed_by_strategy["desktop"],
        pagespeed_mobile=pagespeed_by_strategy["mobile"],
        content_keywords=content_keywords,
        discovery_keywords=discovery_keywords,
    )


def _content_keyword_analytics(
    db: Session,
    *,
    organization_id: Any,
    crawled_websites: int,
) -> WebsiteContentKeywordAnalytics:
    rows = db.execute(
        select(
            WebsiteCorpusAnalysis.words,
            WebsiteCorpusAnalysis.algorithm_version,
            WebsiteCorpusAnalysis.source_crawled_at,
            WebsiteCrawl.crawled_at,
        )
        .join(
            Website,
            (Website.id == WebsiteCorpusAnalysis.website_id)
            & (Website.organization_id == WebsiteCorpusAnalysis.organization_id),
        )
        .join(WebsiteCrawl, WebsiteCrawl.id == Website.crawl_id)
        .where(
            Website.organization_id == organization_id,
            WebsiteCorpusAnalysis.organization_id == organization_id,
            WebsiteCrawl.organization_id == organization_id,
        )
        .execution_options(yield_per=500)
    )
    word_counts: dict[str, int] = {}
    phrase_counts: dict[str, int] = {}
    analyzed_websites = 0
    for row in rows:
        if (
            row.algorithm_version != CORPUS_ANALYSIS_VERSION
            or row.source_crawled_at is None
            or _as_utc(row.source_crawled_at) != _as_utc(row.crawled_at)
        ):
            continue
        analyzed_websites += 1
        per_site_words: set[str] = set()
        per_site_phrases: set[str] = set()
        for item in row.words or []:
            if not isinstance(item, dict):
                continue
            value = item.get("word")
            if not isinstance(value, str):
                continue
            keyword = " ".join(value.casefold().split())
            if not keyword:
                continue
            target = per_site_phrases if item.get("is_phrase") is True else per_site_words
            target.add(keyword)
        for keyword in per_site_words:
            word_counts[keyword] = word_counts.get(keyword, 0) + 1
        for keyword in per_site_phrases:
            phrase_counts[keyword] = phrase_counts.get(keyword, 0) + 1

    return WebsiteContentKeywordAnalytics(
        analyzed_websites=analyzed_websites,
        crawled_websites=crawled_websites,
        unique_words=len(word_counts),
        unique_phrases=len(phrase_counts),
        top_words=_top_keyword_terms(word_counts, analyzed_websites),
        top_phrases=_top_keyword_terms(phrase_counts, analyzed_websites),
    )


def _top_keyword_terms(
    counts: dict[str, int],
    denominator: int,
) -> list[WebsiteKeywordTerm]:
    return [
        WebsiteKeywordTerm(
            keyword=keyword,
            website_count=count,
            website_percentage=_percentage(count, denominator),
        )
        for keyword, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:TOP_KEYWORD_COUNT]
    ]


def _discovery_keyword_analytics(
    db: Session,
    *,
    organization_id: Any,
    total_websites: int,
) -> WebsiteDiscoveryKeywordAnalytics:
    rows = db.execute(
        select(
            SearchKeyword.id,
            SearchKeyword.keyword,
            website_search_keywords.c.website_id,
        )
        .join(
            website_search_keywords,
            website_search_keywords.c.search_keyword_id == SearchKeyword.id,
        )
        .join(
            Website,
            Website.id == website_search_keywords.c.website_id,
        )
        .where(
            SearchKeyword.organization_id == organization_id,
            Website.organization_id == organization_id,
        )
    )
    query_data: dict[str, dict[str, Any]] = {}
    linked_website_ids: set[Any] = set()
    for row in rows:
        keyword = " ".join(row.keyword.casefold().split())
        if not keyword:
            continue
        linked_website_ids.add(row.website_id)
        aggregate = query_data.setdefault(
            keyword,
            {"website_ids": set(), "search_ids": set()},
        )
        aggregate["website_ids"].add(row.website_id)
        aggregate["search_ids"].add(row.id)

    top_queries = sorted(
        query_data.items(),
        key=lambda item: (
            -len(item[1]["website_ids"]),
            -len(item[1]["search_ids"]),
            item[0],
        ),
    )[:TOP_KEYWORD_COUNT]
    return WebsiteDiscoveryKeywordAnalytics(
        unique_queries=len(query_data),
        linked_websites=len(linked_website_ids),
        top_queries=[
            WebsiteDiscoveryKeyword(
                keyword=keyword,
                website_count=len(aggregate["website_ids"]),
                website_percentage=_percentage(
                    len(aggregate["website_ids"]),
                    total_websites,
                ),
                search_count=len(aggregate["search_ids"]),
            )
            for keyword, aggregate in top_queries
        ],
    )


def _pagespeed_summary(
    category_scores: Any,
) -> WebsiteAnalyticsPageSpeedDevice:
    rows = list(category_scores)
    values_by_category: dict[str, list[int]] = {
        category: [] for category in PAGE_SPEED_CATEGORIES
    }
    performance_values: list[int] = []
    for row in rows:
        scores = row if isinstance(row, dict) else {}
        for category in PAGE_SPEED_CATEGORIES:
            value = scores.get(category)
            if isinstance(value, int) and not isinstance(value, bool):
                values_by_category[category].append(value)
                if category == "performance":
                    performance_values.append(value)

    performance_bands = (
        ("poor", lambda value: value < 50),
        ("needs_improvement", lambda value: 50 <= value < 90),
        ("good", lambda value: value >= 90),
    )
    return WebsiteAnalyticsPageSpeedDevice(
        scanned_websites=len(rows),
        averages={
            category: (
                _rounded(sum(values) / len(values)) if values else None
            )
            for category, values in values_by_category.items()
        },
        performance_distribution=[
            WebsiteAnalyticsBreakdown(
                key=label,
                count=sum(predicate(value) for value in performance_values),
                percentage=_percentage(
                    sum(predicate(value) for value in performance_values),
                    len(performance_values),
                ),
            )
            for label, predicate in performance_bands
        ],
    )


def _percentage(count: int, total: int) -> float:
    return _rounded(count / total * 100) if total else 0.0


def _rounded(value: float) -> float:
    return round(value, 1)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
