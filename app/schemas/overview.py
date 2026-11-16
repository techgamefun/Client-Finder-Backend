from datetime import datetime
from typing import Literal

from pydantic import BaseModel


OverviewRange = Literal["7d", "30d", "90d"]


class OverviewPeriod(BaseModel):
    start: datetime
    end: datetime


class OverviewKpi(BaseModel):
    current: int
    previous: int
    absolute_change: int
    delta_percent: float | None
    sparkline: list[int]
    denominator: int | None = None
    percentage: float | None = None


class OverviewKpis(BaseModel):
    websites_discovered: OverviewKpi
    leads_created: OverviewKpi
    reachable_leads: OverviewKpi
    emails_sent: OverviewKpi


class OverviewFunnelStage(BaseModel):
    key: str
    label: str
    count: int
    conversion_percent: float | None


class OverviewFunnel(BaseModel):
    stages: list[OverviewFunnelStage]
    biggest_drop: str | None


class OverviewSeriesPoint(BaseModel):
    date: str
    websites_discovered: int
    leads_created: int


class OverviewStatus(BaseModel):
    status: str
    count: int
    percentage: float | None


class OverviewCoverageRate(BaseModel):
    count: int
    total: int
    percentage: float | None


class OverviewCoverage(BaseModel):
    companies_total: int
    with_email: OverviewCoverageRate
    with_phone: OverviewCoverageRate
    with_website: OverviewCoverageRate
    with_industry: OverviewCoverageRate
    crawl_success: OverviewCoverageRate


class OverviewQueueHealth(BaseModel):
    queued: int
    running: int
    failed: int


class OverviewJobs(BaseModel):
    crawler: OverviewQueueHealth
    pagespeed: OverviewQueueHealth
    failed_badge_count: int


class OverviewAttentionItem(BaseModel):
    id: str
    count: int
    message: str
    href: str
    metric_key: str


class OverviewOnboardingStep(BaseModel):
    id: str
    label: str
    href: str
    completed: bool


class OverviewOnboarding(BaseModel):
    steps: list[OverviewOnboardingStep]


class OverviewRecentLead(BaseModel):
    id: str
    name: str
    domain: str | None
    contact_count: int
    source_keyword: str | None
    lead_status: str
    created_at: datetime
    last_email_sent_at: datetime | None


class OverviewResponse(BaseModel):
    range: OverviewRange
    previous_range: OverviewPeriod
    kpis: OverviewKpis
    funnel: OverviewFunnel
    series: list[OverviewSeriesPoint]
    status_breakdown: list[OverviewStatus]
    coverage: OverviewCoverage
    jobs: OverviewJobs
    attention: list[OverviewAttentionItem]
    onboarding: OverviewOnboarding | None
    recent_leads: list[OverviewRecentLead]


class OverviewUserProgress(BaseModel):
    user_id: str
    email: str
    role: str
    is_active: bool
    websites_discovered: int
    companies_created: int
    leads_created: int
    crawls_completed: int
    emails_sent: int


class AdminOverviewResponse(OverviewResponse):
    user_progress: list[OverviewUserProgress]
