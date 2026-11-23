from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from urllib.parse import parse_qs, urlsplit

from app import main
from conftest import TestingSessionLocal
from app.models import (
    Company,
    Contact,
    Organization,
    OrganizationMembership,
    SearchKeyword,
    User,
    Website,
    WebsiteCrawl,
    WebsiteCrawlJob,
)
from app.services import overview as overview_service
from app.tenant_context import TenantContext


def _invite_member(client: TestClient, email: str, password: str) -> str:
    invitation = client.post(
        "/api/v1/auth/invitations",
        json={"email": email, "role": "user"},
    )
    assert invitation.status_code == 201
    token = parse_qs(urlsplit(invitation.json()["invitation_url"]).query)["token"][0]
    with TestClient(main.app) as invitee:
        accepted = invitee.post(
            f"/api/v1/auth/invitations/{token}/accept",
            json={
                "first_name": "Overview",
                "last_name": "Member",
                "password": password,
            },
        )
    assert accepted.status_code == 200
    return accepted.json()["user"]["id"]


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
    assert data["funnel"]["biggest_drop"] == (
        "Largest drop-off: 100% between Has contact and Emailed."
    )
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


def test_personal_overview_scopes_records_and_admin_overview_shows_team(
    client,
):
    now = datetime.now(timezone.utc)
    with TestingSessionLocal() as db:
        context = db.info["tenant_context"]
        other_user = User(
            organization_id=context.organization_id,
            email="member@example.test",
            password_hash="unused-test-hash",
            role="user",
        )
        db.add(other_user)
        db.flush()
        db.add(
            OrganizationMembership(
                organization_id=context.organization_id,
                user_id=other_user.id,
                role="user",
            )
        )
        db.add_all(
            [
                SearchKeyword(
                    keyword="admin discovery",
                    searched_at=now,
                    created_by_user_id=context.user_id,
                ),
                SearchKeyword(
                    keyword="member discovery",
                    searched_at=now,
                    created_by_user_id=other_user.id,
                ),
                Company(
                    name="Admin-owned company",
                    domain="admin-owned.example",
                    created_by_user_id=context.user_id,
                    created_at=now,
                ),
                Company(
                    name="Member-owned company",
                    domain="member-owned.example",
                    created_by_user_id=other_user.id,
                    created_at=now,
                ),
                Website(
                    website_url="https://admin-owned.example/",
                    domain="admin-owned.example",
                    created_by_user_id=context.user_id,
                    created_at=now,
                ),
                Website(
                    website_url="https://member-owned.example/",
                    domain="member-owned.example",
                    created_by_user_id=other_user.id,
                    created_at=now,
                ),
            ]
        )
        db.flush()
        companies = db.query(Company).order_by(Company.name).all()
        db.add_all(
            [
                Contact(
                    company_id=company.id,
                    created_by_user_id=company.created_by_user_id,
                    business_email=f"hello@{company.domain}",
                )
                for company in companies
            ]
        )
        db.commit()

    personal = client.get("/api/v1/overview?range=30d&tz=UTC")
    assert personal.status_code == 200
    personal_data = personal.json()
    assert personal_data["kpis"]["websites_discovered"]["current"] == 1
    assert personal_data["kpis"]["leads_created"]["current"] == 1
    assert personal_data["coverage"]["companies_total"] == 1
    assert [lead["name"] for lead in personal_data["recent_leads"]] == [
        "Admin-owned company"
    ]

    admin = client.get("/api/v1/overview/admin?range=30d&tz=UTC")
    assert admin.status_code == 200
    admin_data = admin.json()
    assert admin_data["kpis"]["websites_discovered"]["current"] == 2
    assert admin_data["kpis"]["leads_created"]["current"] == 2
    progress_by_email = {
        row["email"]: row for row in admin_data["user_progress"]
    }
    assert progress_by_email["admin@example.test"]["websites_discovered"] == 1
    assert progress_by_email["member@example.test"]["websites_discovered"] == 1
    assert progress_by_email["admin@example.test"]["searches_run"] == 1
    assert progress_by_email["member@example.test"]["searches_run"] == 1
    assert progress_by_email["member@example.test"]["leads_created"] == 1


def test_admin_overview_endpoint_requires_admin(client):
    member_id = _invite_member(
        client,
        "overview-member@example.test",
        "overview-member-password",
    )

    with TestClient(main.app) as member_client:
        login = member_client.post(
            "/api/v1/auth/login",
            json={
                "email": "overview-member@example.test",
                "password": "overview-member-password",
            },
        )
        assert login.status_code == 200
        response = member_client.get(
            "/api/v1/overview/admin?range=30d&tz=UTC"
        )
        member_activity = member_client.get(
            f"/api/v1/overview/admin/users/{member_id}/activity"
        )

    assert response.status_code == 403
    assert member_activity.status_code == 403


def test_admin_member_activity_returns_daily_service_counts(
    client,
    monkeypatch,
):
    now = datetime(2025, 6, 30, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(overview_service, "_utc_now", lambda: now)

    with TestingSessionLocal() as db:
        context = db.info["tenant_context"]
        member = User(
            organization_id=context.organization_id,
            email="activity-member@example.test",
            password_hash="unused-test-hash",
            role="user",
        )
        db.add(member)
        db.flush()
        db.add(
            OrganizationMembership(
                organization_id=context.organization_id,
                user_id=member.id,
                role="user",
            )
        )
        company = Company(
            name="Activity Company",
            domain="activity.example",
            created_by_user_id=member.id,
            created_at=now - timedelta(days=2),
        )
        db.add_all(
            [
                company,
                SearchKeyword(
                    keyword="activity search",
                    searched_at=now - timedelta(days=3),
                    created_by_user_id=member.id,
                ),
                Website(
                    website_url="https://activity.example/",
                    domain="activity.example",
                    created_by_user_id=member.id,
                    created_at=now - timedelta(days=2),
                ),
                WebsiteCrawlJob(
                    website_url="https://activity.example/",
                    status="succeeded",
                    created_by_user_id=member.id,
                    completed_at=now - timedelta(days=1),
                ),
            ]
        )
        db.flush()
        db.add(
            Contact(
                company_id=company.id,
                created_by_user_id=member.id,
                business_email="hello@activity.example",
            )
        )
        member_id = member.id
        db.commit()

    response = client.get(
        f"/api/v1/overview/admin/users/{member_id}/activity"
        "?range=7d&tz=UTC"
    )

    assert response.status_code == 200
    data = response.json()
    assert data["member"]["email"] == "activity-member@example.test"
    assert data["range"] == "7d"
    assert data["timezone"] == "UTC"
    assert data["member"]["searches_run"] == 1
    assert data["member"]["websites_discovered"] == 1
    assert data["member"]["companies_created"] == 1
    assert data["member"]["leads_created"] == 1
    assert data["member"]["crawls_completed"] == 1
    assert len(data["series"]) == 7
    assert data["series"][0]["date"] == "2025-06-24"
    assert data["series"][-1]["date"] == "2025-06-30"
    assert data["series"][0]["searches_run"] == 0
    assert data["series"][3]["searches_run"] == 1
    assert data["series"][4]["websites_discovered"] == 1
    assert data["series"][4]["companies_created"] == 1
    assert data["series"][4]["leads_created"] == 1
    assert data["series"][5]["crawls_completed"] == 1
    assert sum(point["searches_run"] for point in data["series"]) == 1
    assert sum(point["emails_sent"] for point in data["series"]) == 0

    missing_member = client.get(
        f"/api/v1/overview/admin/users/{uuid4()}/activity?range=30d&tz=UTC"
    )
    assert missing_member.status_code == 404


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
