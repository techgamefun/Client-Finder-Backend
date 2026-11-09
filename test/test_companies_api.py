
from uuid import UUID

from sqlalchemy import select

from conftest import TestingSessionLocal
from app.models import Company, Contact, LeadSource, WebsiteCrawl
from app.services import research_source_crawler, website_crawler


def test_create_company(client):
    response = client.post(
        "/api/v1/companies",
        json={
            "name": "Example Manufacturing Pvt Ltd",
            "website": "https://example.com",
            "domain": "example.com",
            "industry": "Manufacturing",
            "city": "Kolkata",
            "state": "West Bengal",
        },
    )

    assert response.status_code == 201

    data = response.json()
    assert data["name"] == "Example Manufacturing Pvt Ltd"
    assert data["domain"] == "example.com"
    UUID(data["id"])


def test_list_companies(client):
    client.post(
        "/api/v1/companies",
        json={
            "name": "Example Company",
            "domain": "example.com",
        },
    )

    response = client.get("/api/v1/companies")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["name"] == "Example Company"


def test_duplicate_domain_returns_conflict(client):
    payload = {
        "name": "Example Company",
        "domain": "example.com",
    }

    first_response = client.post(
        "/api/v1/companies",
        json=payload,
    )

    second_response = client.post(
        "/api/v1/companies",
        json={
            "name": "Another Company",
            "domain": "example.com",
        },
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 409


def test_invalid_company_is_rejected(client):
    response = client.post(
        "/api/v1/companies",
        json={
            "name": "",
            "website": "not-a-url",
        },
    )

    assert response.status_code == 422


def test_company_detail_update_status_and_delete(client):
    created = client.post(
        "/api/v1/companies",
        json={"name": "Example Company", "website": "https://example.com"},
    )
    assert created.status_code == 201
    company_id = UUID(created.json()["id"])

    with TestingSessionLocal() as db:
        db.add(
            Contact(
                company_id=company_id,
                name="Alex Example",
                role="Operations",
                business_email="alex@example.com",
                business_phone="+1 555 0100",
                is_publicly_listed=True,
            )
        )
        db.add(
            LeadSource(
                company_id=company_id,
                source_url="https://example.com/about",
                source_type="web_search",
                evidence="Company profile",
            )
        )
        db.commit()

    detail = client.get(f"/api/v1/companies/{company_id}")
    assert detail.status_code == 200
    assert detail.json()["contacts"][0]["business_email"] == "alex@example.com"
    assert detail.json()["sources"][0]["evidence"] == "Company profile"

    updated = client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "name": "Example Group",
            "website": "https://www.example.org",
            "domain": "www.example.org",
            "industry": "Technology",
            "city": "Toronto",
            "state": "Ontario",
            "software_need": "CRM",
            "lead_score": 8,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Example Group"
    assert updated.json()["domain"] == "example.org"
    assert updated.json()["lead_score"] == 8
    assert updated.json()["contacts"][0]["name"] == "Alex Example"

    status_update = client.patch(
        f"/api/v1/companies/{company_id}/status",
        json={"lead_status": "qualified"},
    )
    assert status_update.status_code == 200
    assert status_update.json()["lead_status"] == "qualified"

    deleted = client.delete(f"/api/v1/companies/{company_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/companies/{company_id}").status_code == 404

    with TestingSessionLocal() as db:
        assert db.get(Company, company_id) is None
        assert db.scalars(select(Contact)).all() == []
        assert db.scalars(select(LeadSource)).all() == []


def test_company_update_rejects_duplicate_domain_without_partial_write(client):
    first = client.post(
        "/api/v1/companies",
        json={"name": "First Company", "domain": "first.example"},
    )
    second = client.post(
        "/api/v1/companies",
        json={"name": "Second Company", "domain": "second.example"},
    )

    second_id = second.json()["id"]
    response = client.patch(
        f"/api/v1/companies/{second_id}",
        json={"name": "Changed Name", "domain": "first.example"},
    )

    assert response.status_code == 409
    unchanged = client.get(f"/api/v1/companies/{second_id}")
    assert unchanged.json()["name"] == "Second Company"
    assert unchanged.json()["domain"] == "second.example"


def test_lead_status_rejects_blank_values(client):
    created = client.post(
        "/api/v1/companies",
        json={"name": "Example Company"},
    )

    response = client.patch(
        f"/api/v1/companies/{created.json()['id']}/status",
        json={"lead_status": "   "},
    )

    assert response.status_code == 422


def test_company_website_crawl_status_and_recrawl(client, monkeypatch):
    created = client.post(
        "/api/v1/companies",
        json={"name": "Example Company", "website": "https://example.com"},
    )
    company_id = created.json()["id"]

    first_status = client.get(
        f"/api/v1/companies/{company_id}/website-crawl"
    )
    assert first_status.status_code == 200
    assert first_status.json() is None

    crawl_count = 0

    async def fake_crawl_website(website_url: str):
        nonlocal crawl_count
        crawl_count += 1
        return website_crawler.CrawlData(
            website_url=website_url.rstrip("/") + "/",
            about_page_url="https://example.com/about",
            about_content=f"Crawl {crawl_count}",
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
            emails=["hello@example.com"],
            social_links=["https://www.linkedin.com/company/example"],
        )

    monkeypatch.setattr(website_crawler, "crawl_website", fake_crawl_website)

    first_crawl = client.post(
        f"/api/v1/companies/{company_id}/website-crawl"
    )
    assert first_crawl.status_code == 200
    assert first_crawl.json()["about_content"] == "Crawl 1"

    status_after_crawl = client.get(
        f"/api/v1/companies/{company_id}/website-crawl"
    )
    assert status_after_crawl.status_code == 200
    assert status_after_crawl.json()["id"] == first_crawl.json()["id"]
    assert status_after_crawl.json()["emails"] == ["hello@example.com"]

    recrawl = client.post(
        f"/api/v1/companies/{company_id}/website-crawl"
    )
    assert recrawl.status_code == 200
    assert recrawl.json()["id"] == first_crawl.json()["id"]
    assert recrawl.json()["about_content"] == "Crawl 2"


def test_company_website_crawl_requires_website_or_domain(client):
    created = client.post(
        "/api/v1/companies",
        json={"name": "Website Missing Company"},
    )
    company_id = created.json()["id"]

    response = client.post(
        f"/api/v1/companies/{company_id}/website-crawl"
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Add a company website or domain before crawling."
    )


def test_company_website_crawl_falls_back_to_domain(client, monkeypatch):
    created = client.post(
        "/api/v1/companies",
        json={"name": "Domain Company", "domain": "example.com"},
    )
    crawled_urls = []

    async def fake_crawl_website(website_url: str):
        crawled_urls.append(website_url)
        return website_crawler.CrawlData(
            website_url="https://example.com/",
            about_page_url=None,
            about_content=None,
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
            emails=[],
            social_links=[],
        )

    monkeypatch.setattr(website_crawler, "crawl_website", fake_crawl_website)

    response = client.post(
        f"/api/v1/companies/{created.json()['id']}/website-crawl"
    )

    assert response.status_code == 200
    assert crawled_urls == ["https://example.com"]


def test_research_source_crawl_fills_missing_company_details(
    client,
    monkeypatch,
):
    created = client.post(
        "/api/v1/companies",
        json={"name": "Example Company"},
    )
    company_id = created.json()["id"]

    async def fake_crawl_website(source_url: str):
        return website_crawler.CrawlData(
            website_url=source_url,
            about_page_url=None,
            about_content="Example Company manufactures equipment.",
            contact_page_url=source_url,
            contact_content="Contact support@example.com.",
            goals_page_url=None,
            goals_content=None,
            emails=["support@example.com"],
            social_links=[],
            phones=["+1 555 0100"],
        )

    monkeypatch.setattr(
        research_source_crawler,
        "crawl_website",
        fake_crawl_website,
    )

    response = client.post(
        f"/api/v1/companies/{company_id}/sources/crawl",
        json={"source_url": "https://example.com/about"},
    )

    assert response.status_code == 200
    company = response.json()
    assert company["website"] == "https://example.com/"
    assert company["domain"] == "example.com"
    assert company["sources"][0]["source_type"] == "company_research"
    assert company["sources"][0]["verified_at"] is not None
    assert {
        (contact["business_email"], contact["business_phone"])
        for contact in company["contacts"]
    } == {
        ("support@example.com", None),
        (None, "+1 555 0100"),
    }

    with TestingSessionLocal() as db:
        assert db.scalar(select(WebsiteCrawl)).website_url == (
            "https://example.com/about"
        )


def test_research_source_crawl_does_not_assign_unrelated_publisher_details(
    client,
    monkeypatch,
):
    created = client.post(
        "/api/v1/companies",
        json={"name": "Example Manufacturing"},
    )
    company_id = created.json()["id"]

    async def fake_crawl_website(source_url: str):
        return website_crawler.CrawlData(
            website_url=source_url,
            about_page_url=None,
            about_content="A publisher article about Example Manufacturing.",
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
            emails=["editor@publisher.example"],
            social_links=[],
            phones=["+1 555 0199"],
        )

    monkeypatch.setattr(
        research_source_crawler,
        "crawl_website",
        fake_crawl_website,
    )

    response = client.post(
        f"/api/v1/companies/{company_id}/sources/crawl",
        json={"source_url": "https://publisher.example/article"},
    )

    assert response.status_code == 200
    company = response.json()
    assert company["website"] is None
    assert company["domain"] is None
    assert company["contacts"] == []
    assert len(company["sources"]) == 1


def test_research_source_crawl_preserves_existing_company_details(
    client,
    monkeypatch,
):
    created = client.post(
        "/api/v1/companies",
        json={
            "name": "Example Company",
            "website": "https://existing.example",
            "domain": "existing.example",
        },
    )
    company_id = created.json()["id"]

    async def fake_crawl_website(source_url: str):
        return website_crawler.CrawlData(
            website_url=source_url,
            about_page_url=None,
            about_content=None,
            contact_page_url=None,
            contact_content="support@example.com",
            goals_page_url=None,
            goals_content=None,
            emails=["support@example.com"],
            social_links=[],
        )

    monkeypatch.setattr(
        research_source_crawler,
        "crawl_website",
        fake_crawl_website,
    )

    response = client.post(
        f"/api/v1/companies/{company_id}/sources/crawl",
        json={"source_url": "https://example.com/contact"},
    )

    assert response.status_code == 200
    company = response.json()
    assert company["website"] == "https://existing.example/"
    assert company["domain"] == "existing.example"
    assert [contact["business_email"] for contact in company["contacts"]] == [
        "support@example.com"
    ]
