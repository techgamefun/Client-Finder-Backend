from datetime import datetime, timedelta, timezone

from conftest import TestingSessionLocal
from app.models import (
    Company,
    Contact,
    Organization,
    SearchKeyword,
    Website,
    WebsiteCrawl,
    WebsiteCrawlJob,
)
from app.services import overview as overview_service
from app.tenant_context import TenantContext


def test_overview_empty_organization(client):
    response = client.get("/api/v1/overview?range=30d&tz=Asia/Kolkata")

    assert response.status_code == 200
    data = response.json()
    assert data["range"] == "30d"
    assert data["kpis"]["websites_discovered"]["current"] == 0
    assert data["kpis"]["websites_discovered"]["delta_percent"] is None
    assert data["kpis"]["reachable_leads"]["percentage"] is None
    assert data["funnel"]["stages"][0]["count"] == 0
    assert data["status_breakdown"] == []
    assert data["coverage"]["with_email"]["percentage"] is None
    assert data["jobs"]["failed_badge_count"] == 0
    assert data["attention"] == []
    assert data["recent_leads"] == []
    assert data["onboarding"] is not None


def test_overview_populated_organization_periods_and_funnel(
    client,
    monkeypatch,
):
    now = datetime(2025, 6, 30, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(overview_service, "_utc_now", lambda: now)

    with TestingSessionLocal() as db:
        crawl = WebsiteCrawl(website_url="https://current.example/")
        current_company = Company(
            name="Current Example",
            website="https://current.example/",
            domain="current.example",
            industry="Software",
            lead_status="qualified",
            created_at=now - timedelta(days=10),
        )
        previous_company = Company(
            name="Previous Example",
            domain="previous.example",
            lead_status="new",
            created_at=now - timedelta(days=40),
        )
        company_without_contact = Company(
            name="Company Without Contact",
            domain="no-contact.example",
            created_at=now - timedelta(days=5),
        )
        db.add_all(
            [crawl, current_company, previous_company, company_without_contact]
        )
        db.flush()
        db.add_all(
            [
                Contact(
                    company_id=current_company.id,
                    business_email="hello@current.example",
                ),
                Contact(company_id=previous_company.id),
            ]
        )
        search_keyword = SearchKeyword(
            keyword="software companies",
            searched_at=now - timedelta(days=10),
        )
        current_website = Website(
            website_url="https://current.example/",
            domain="current.example",
            crawl_id=crawl.id,
            status="lead",
            created_at=now - timedelta(days=10),
        )
        current_website.search_keywords.append(search_keyword)
        db.add_all(
            [
                search_keyword,
                current_website,
                Website(
                    website_url="https://previous.example/",
                    domain="previous.example",
                    created_at=now - timedelta(days=40),
                ),
                WebsiteCrawlJob(
                    website_url="https://failed.example/",
                    status="failed",
                    created_at=now - timedelta(hours=2),
                ),
            ]
        )
        db.commit()

    response = client.get("/api/v1/overview?range=30d&tz=UTC")

    assert response.status_code == 200
    data = response.json()
    assert data["kpis"]["websites_discovered"]["current"] == 1
    assert data["kpis"]["websites_discovered"]["previous"] == 1
    assert data["kpis"]["websites_discovered"]["absolute_change"] == 0
    assert data["kpis"]["leads_created"]["current"] == 1
    assert data["kpis"]["leads_created"]["previous"] == 1
    assert data["kpis"]["reachable_leads"]["current"] == 1
    assert data["kpis"]["reachable_leads"]["denominator"] == 2
    assert data["kpis"]["reachable_leads"]["percentage"] == 50.0
    assert data["funnel"]["stages"][0]["count"] == 1
    assert data["funnel"]["stages"][1]["count"] == 1
    assert data["funnel"]["stages"][2]["count"] == 1
    assert data["funnel"]["stages"][3]["count"] == 1
    assert data["funnel"]["stages"][4]["count"] == 0
    assert data["jobs"]["failed_badge_count"] == 1
    assert data["coverage"]["with_email"] == {
        "count": 1,
        "total": 3,
        "percentage": 33.3,
    }
    assert data["recent_leads"][0]["source_keyword"] == "software companies"
    assert data["onboarding"] is not None
    assert data["onboarding"]["steps"][2]["completed"] is True
    assert data["onboarding"]["steps"][3]["completed"] is True


def test_overview_does_not_include_another_organizations_data(client):
    with TestingSessionLocal() as db:
        first_context = db.info["tenant_context"]
        other_organization = Organization(name="Another organization")
        db.add(other_organization)
        db.flush()
        db.info["tenant_context"] = TenantContext(
            organization_id=other_organization.id,
            user_id=None,
            email="other@example.test",
            role="admin",
        )
        other_company = Company(
            name="Private company",
            domain="private.example",
            created_at=datetime.now(timezone.utc),
        )
        db.add(other_company)
        db.flush()
        db.add(
            Contact(
                company_id=other_company.id,
                business_email="private@private.example",
            )
        )
        db.commit()
        db.info["tenant_context"] = first_context

    response = client.get("/api/v1/overview?range=30d&tz=UTC")

    assert response.status_code == 200
    data = response.json()
    assert data["kpis"]["leads_created"]["current"] == 0
    assert data["coverage"]["companies_total"] == 0
    assert data["recent_leads"] == []


def test_overview_delta_handles_zero_previous_value():
    new_metric = overview_service._metric(4, 0, [0] * 30)
    unchanged_empty_metric = overview_service._metric(0, 0, [0] * 30)

    assert new_metric.absolute_change == 4
    assert new_metric.delta_percent is None
    assert unchanged_empty_metric.absolute_change == 0
    assert unchanged_empty_metric.delta_percent is None


def test_overview_onboarding_disappears_when_all_steps_are_complete():
    onboarding = overview_service._build_onboarding(
        websites_total=1,
        companies_total=1,
        has_search=True,
        has_tavily_key=True,
        has_assigned_gmail=True,
        has_sent_campaign=True,
    )

    assert onboarding is None
