from datetime import datetime

from pydantic import BaseModel


class WebsiteAnalyticsBreakdown(BaseModel):
    key: str
    count: int
    percentage: float


class WebsiteAnalyticsCoverage(BaseModel):
    domain_rating: int
    hypestat: int
    pagespeed_any: int
    pagespeed_desktop: int
    pagespeed_mobile: int


class WebsiteAnalyticsDomainRating(BaseModel):
    count: int
    average: float | None
    median: float | None
    distribution: list[WebsiteAnalyticsBreakdown]


class WebsiteAnalyticsPageSpeedDevice(BaseModel):
    scanned_websites: int
    averages: dict[str, float | None]
    performance_distribution: list[WebsiteAnalyticsBreakdown]


class WebsiteKeywordTerm(BaseModel):
    keyword: str
    website_count: int
    website_percentage: float


class WebsiteDiscoveryKeyword(BaseModel):
    keyword: str
    website_count: int
    website_percentage: float
    search_count: int


class WebsiteContentKeywordAnalytics(BaseModel):
    analyzed_websites: int
    crawled_websites: int
    unique_words: int
    unique_phrases: int
    top_words: list[WebsiteKeywordTerm]
    top_phrases: list[WebsiteKeywordTerm]


class WebsiteDiscoveryKeywordAnalytics(BaseModel):
    unique_queries: int
    linked_websites: int
    top_queries: list[WebsiteDiscoveryKeyword]


class WebsiteAnalyticsResponse(BaseModel):
    generated_at: datetime
    total_websites: int
    crawled_websites: int
    crawled_percentage: float | None
    coverage: WebsiteAnalyticsCoverage
    status_breakdown: list[WebsiteAnalyticsBreakdown]
    discovery_source_breakdown: list[WebsiteAnalyticsBreakdown]
    site_type_breakdown: list[WebsiteAnalyticsBreakdown]
    page_type_breakdown: list[WebsiteAnalyticsBreakdown]
    domain_rating: WebsiteAnalyticsDomainRating
    pagespeed_desktop: WebsiteAnalyticsPageSpeedDevice
    pagespeed_mobile: WebsiteAnalyticsPageSpeedDevice
    content_keywords: WebsiteContentKeywordAnalytics
    discovery_keywords: WebsiteDiscoveryKeywordAnalytics
