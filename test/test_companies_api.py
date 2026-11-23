
import asyncio
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from conftest import TestingSessionLocal
from app import worker as website_crawl_worker
from app.models import Company, Contact
from app.services.pagespeed import PageSpeedScanData
from app.services import website_crawler


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


def test_company_pipeline_filters_and_paginates_on_server(client):
    with TestingSessionLocal() as db:
        matched = Company(
            name="Acme Health",
            domain="acme-health.test",
            lead_status="qualified",
            last_email_sent_at=datetime.now(timezone.utc),
        )
        other_lead = Company(
            name="Acme Retail",
            domain="acme-retail.test",
            lead_status="new",
        )
        company_without_contact = Company(
            name="Acme Uncontacted",
            domain="acme-uncontacted.test",
            lead_status="qualified",
        )
        db.add_all([matched, other_lead, company_without_contact])
        db.flush()
        db.add(Contact(company_id=matched.id))
        db.add(Contact(company_id=other_lead.id))
        matched_id = matched.id
        db.commit()

    response = client.get(
        "/api/v1/companies/pipeline",
        params={
            "view": "leads",
            "query": "ACME",
            "status": "qualified",
            "email_status": "contacted",
            "website": "with_website",
            "page": 8,
            "page_size": 10,
        },
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert [item["id"] for item in result["items"]] == [str(matched_id)]
    assert result["total_count"] == 1
    assert result["eligible_count"] == 2
    assert result["company_count"] == 3
    assert result["status_counts"] == {"new": 1, "qualified": 1}
    assert result["page"] == 1
    assert result["page_count"] == 1


def test_company_pipeline_selection_returns_all_server_filtered_leads(client):
    with TestingSessionLocal() as db:
        first = Company(name="Selected Alpha", lead_status="qualified")
        second = Company(name="Selected Beta", lead_status="qualified")
        excluded = Company(name="Other Company", lead_status="new")
        db.add_all([first, second, excluded])
        db.flush()
        db.add_all(
            [
                Contact(company_id=first.id),
                Contact(company_id=second.id),
                Contact(company_id=excluded.id),
            ]
        )
        first_id = first.id
        second_id = second.id
        db.commit()

    response = client.get(
        "/api/v1/companies/pipeline/selection",
        params={
            "view": "leads",
            "query": "selected",
            "status": "qualified",
            "email_status": "all",
            "website": "all",
        },
    )

    assert response.status_code == 200
    assert response.json()["total_count"] == 2
    assert set(response.json()["company_ids"]) == {
        str(first_id),
        str(second_id),
    }


def test_lead_groups_can_be_named_renamed_and_manage_members(client):
    first = client.post(
        "/api/v1/companies",
        json={"name": "First grouped lead"},
    ).json()
    second = client.post(
        "/api/v1/companies",
        json={"name": "Second grouped lead"},
    ).json()

    created = client.post(
        "/api/v1/companies/lead-batches",
        json={
            "name": "  Regional prospects  ",
            "company_ids": [first["id"]],
        },
    )
    empty_group = client.post(
        "/api/v1/companies/lead-batches",
        json={"name": "Later follow-up"},
    )
    assert created.status_code == 201
    assert created.json()["name"] == "Regional prospects"
    assert empty_group.status_code == 201
    initial_group_id = created.json()["id"]
    empty_group_id = empty_group.json()["id"]

    add_response = client.post(
        "/api/v1/companies/lead-batches/members",
        json={
            "action": "add",
            "batch_id": initial_group_id,
            "company_ids": [second["id"]],
        },
    )
    assert add_response.status_code == 200
    assert add_response.json()["updated_count"] == 1

    move_response = client.post(
        "/api/v1/companies/lead-batches/members",
        json={
            "action": "move",
            "batch_id": empty_group_id,
            "company_ids": [first["id"]],
        },
    )
    assert move_response.status_code == 200
    assert move_response.json()["updated_count"] == 1

    ungroup_response = client.post(
        "/api/v1/companies/lead-batches/members",
        json={
            "action": "ungroup",
            "company_ids": [second["id"]],
        },
    )
    assert ungroup_response.status_code == 200
    assert ungroup_response.json()["updated_count"] == 1

    renamed = client.patch(
        f"/api/v1/companies/lead-batches/{initial_group_id}",
        json={"name": "  Qualified prospects "},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Qualified prospects"

    with TestingSessionLocal() as db:
        first_company = db.scalar(
            select(Company)
            .options(selectinload(Company.lead_batches))
            .where(Company.id == UUID(first["id"]))
        )
        second_company = db.scalar(
            select(Company)
            .options(selectinload(Company.lead_batches))
            .where(Company.id == UUID(second["id"]))
        )
        assert first_company is not None
        assert second_company is not None
        assert [batch.id for batch in first_company.lead_batches] == [
            UUID(empty_group_id),
        ]
        assert second_company.lead_batches == []

    invalid_name = client.patch(
        f"/api/v1/companies/lead-batches/{empty_group_id}",
        json={"name": "   "},
    )
    assert invalid_name.status_code == 422


def test_company_pipeline_returns_only_requested_page(client):
    with TestingSessionLocal() as db:
        companies = [Company(name=f"Page Lead {index:02}") for index in range(12)]
        db.add_all(companies)
        db.flush()
        db.add_all([Contact(company_id=company.id) for company in companies])
        db.commit()

    response = client.get(
        "/api/v1/companies/pipeline",
        params={
            "view": "leads",
            "page": 2,
            "page_size": 10,
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert len(result["items"]) == 2
    assert result["total_count"] == 12
    assert result["page"] == 2
    assert result["page_count"] == 2


def test_company_pipeline_rejects_unsupported_page_size(client):
    response = client.get(
        "/api/v1/companies/pipeline",
        params={"page_size": 11},
    )

    assert response.status_code == 422


def test_company_navigation_summary_counts_review_leads_with_contacts(
    client,
):
    with TestingSessionLocal() as db:
        review_with_contact = Company(
            name="Review with contact",
            lead_status="needs_review",
        )
        review_without_contact = Company(
            name="Review without contact",
            lead_status="needs_review",
        )
        new_with_contact = Company(
            name="New with contact",
            lead_status="new",
        )
        db.add_all(
            [
                review_with_contact,
                review_without_contact,
                new_with_contact,
            ]
        )
        db.flush()
        db.add_all(
            [
                Contact(company_id=review_with_contact.id),
                Contact(company_id=new_with_contact.id),
            ]
        )
        db.commit()

    response = client.get("/api/v1/companies/navigation-summary")

    assert response.status_code == 200
    assert response.json() == {"leads_needing_review": 1}


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


def test_company_contact_crud_and_company_scoping(client):
    company = client.post(
        "/api/v1/companies",
        json={"name": "Contact CRUD Company"},
    )
    other_company = client.post(
        "/api/v1/companies",
        json={"name": "Other Contact Company"},
    )
    company_id = company.json()["id"]
    other_company_id = other_company.json()["id"]

    created = client.post(
        f"/api/v1/companies/{company_id}/contacts",
        json={
            "name": "  Alex Example  ",
            "role": " Operations ",
            "business_email": "alex@example.com",
            "business_phone": " +1 555 0100 ",
            "is_publicly_listed": True,
        },
    )
    assert created.status_code == 201
    contact = created.json()
    contact_id = contact["id"]
    assert contact["company_id"] == company_id
    assert contact["name"] == "Alex Example"
    assert contact["role"] == "Operations"
    assert contact["business_email"] == "alex@example.com"
    assert contact["business_phone"] == "+1 555 0100"
    assert contact["is_publicly_listed"] is True

    listed = client.get(f"/api/v1/companies/{company_id}/contacts")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [contact_id]

    fetched = client.get(
        f"/api/v1/companies/{company_id}/contacts/{contact_id}"
    )
    assert fetched.status_code == 200
    assert fetched.json() == contact

    updated = client.patch(
        f"/api/v1/companies/{company_id}/contacts/{contact_id}",
        json={
            "name": "Alex A. Example",
            "business_email": None,
            "is_publicly_listed": False,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Alex A. Example"
    assert updated.json()["business_email"] is None
    assert updated.json()["business_phone"] == "+1 555 0100"
    assert updated.json()["is_publicly_listed"] is False

    assert client.get(
        f"/api/v1/companies/{other_company_id}/contacts/{contact_id}"
    ).status_code == 404
    assert client.patch(
        f"/api/v1/companies/{company_id}/contacts/{contact_id}",
        json={},
    ).status_code == 422
    assert client.post(
        f"/api/v1/companies/{company_id}/contacts",
        json={"business_email": "not-an-email"},
    ).status_code == 422
    assert client.post(
        f"/api/v1/companies/{company_id}/contacts",
        json={"name": "  "},
    ).status_code == 422

    deleted = client.delete(
        f"/api/v1/companies/{company_id}/contacts/{contact_id}"
    )
    assert deleted.status_code == 204
    assert client.get(
        f"/api/v1/companies/{company_id}/contacts/{contact_id}"
    ).status_code == 404
    assert client.get(f"/api/v1/companies/{company_id}").json()["contacts"] == []


def test_company_contact_crud_requires_authentication(client):
    company = client.post(
        "/api/v1/companies",
        json={"name": "Contact Auth Company"},
    )
    client.cookies.clear()

    response = client.get(
        f"/api/v1/companies/{company.json()['id']}/contacts"
    )

    assert response.status_code == 401


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
        db.commit()

    detail = client.get(f"/api/v1/companies/{company_id}")
    assert detail.status_code == 200
    assert detail.json()["contacts"][0]["business_email"] == "alex@example.com"
    assert "sources" not in detail.json()
    assert "search_keywords" not in detail.json()

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

    async def fake_enqueue(_job_id: str) -> None:
        return None

    async def fake_crawl_website(website_url: str, **_options):
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
            pages=[
                {
                    "url": "https://example.com/",
                    "classification_signals": {
                        "og_type": "website",
                        "generator": "Shopify",
                        "article_count": 1,
                        "password_input_count": 0,
                        "shopping_elements": {"product_schema": 1},
                        "cta_texts": ["Add to Cart"],
                    },
                }
            ],
        )

    monkeypatch.setattr(
        "app.services.crawl_jobs.enqueue_job",
        fake_enqueue,
    )
    monkeypatch.setattr(
        website_crawl_worker,
        "SessionLocal",
        TestingSessionLocal,
    )
    monkeypatch.setattr(
        website_crawl_worker,
        "crawl_website",
        fake_crawl_website,
    )

    first_crawl = client.post(
        f"/api/v1/companies/{company_id}/website-crawl"
    )
    assert first_crawl.status_code == 202
    asyncio.run(
        website_crawl_worker.process_job(UUID(first_crawl.json()["id"]))
    )

    status_after_crawl = client.get(
        f"/api/v1/companies/{company_id}/website-crawl"
    )
    assert status_after_crawl.status_code == 200
    assert status_after_crawl.json()["about_content"] == "Crawl 1"
    assert status_after_crawl.json()["emails"] == ["hello@example.com"]
    assert (
        status_after_crawl.json()["classification_signals"][0][
            "classification_signals"
        ]["og_type"]
        == "website"
    )

    recrawl = client.post(
        f"/api/v1/companies/{company_id}/website-crawl"
    )
    assert recrawl.status_code == 202
    asyncio.run(website_crawl_worker.process_job(UUID(recrawl.json()["id"])))
    status_after_recrawl = client.get(
        f"/api/v1/companies/{company_id}/website-crawl"
    )
    assert status_after_recrawl.json()["id"] == status_after_crawl.json()["id"]
    assert status_after_recrawl.json()["about_content"] == "Crawl 2"


def test_company_pagespeed_scan_and_rescan(client, monkeypatch):
    created = client.post(
        "/api/v1/companies",
        json={"name": "Example Company", "website": "https://example.com"},
    )
    company_id = created.json()["id"]
    strategies: list[str] = []

    async def fake_scan_pagespeed(
        website_url: str,
        strategy: str,
        _api_key: str | None = None,
    ):
        strategies.append(strategy)
        return PageSpeedScanData(
            website_url=website_url,
            strategy=strategy,
            category_scores={
                "performance": 90,
                "accessibility": 91,
                "best-practices": 92,
                "seo": 93,
            },
        )

    monkeypatch.setattr(
        "app.api.companies.scan_pagespeed",
        fake_scan_pagespeed,
    )

    first_scan = client.post(
        f"/api/v1/companies/{company_id}/pagespeed-scans",
        json={"strategy": "mobile"},
    )
    second_scan = client.post(
        f"/api/v1/companies/{company_id}/pagespeed-scans",
        json={"strategy": "mobile"},
    )

    assert first_scan.status_code == 201
    assert second_scan.status_code == 201
    assert "report" not in first_scan.json()
    assert set(first_scan.json()["category_scores"]) == {
        "performance",
        "accessibility",
        "best-practices",
        "seo",
    }
    assert first_scan.json()["strategy"] == "mobile"
    assert first_scan.json()["category_scores"]["performance"] == 90
    assert first_scan.json()["id"] != second_scan.json()["id"]
    assert strategies == ["mobile", "mobile"]

    company = client.get(f"/api/v1/companies/{company_id}")
    assert len(company.json()["pagespeed_scans"]) == 2
    scan_id = first_scan.json()["id"]
    scan_response = client.get(
        f"/api/v1/companies/{company_id}/pagespeed-scans/{scan_id}"
    )
    assert scan_response.status_code == 200
    assert "report" not in scan_response.json()


def test_company_pagespeed_scan_requires_website(client):
    created = client.post(
        "/api/v1/companies",
        json={"name": "Example Company"},
    )

    response = client.post(
        f"/api/v1/companies/{created.json()['id']}/pagespeed-scans",
        json={"strategy": "mobile"},
    )

    assert response.status_code == 422


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

    async def fake_crawl_website(website_url: str, **_options):
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

    async def fake_enqueue(_job_id: str) -> None:
        return None

    monkeypatch.setattr(
        "app.services.crawl_jobs.enqueue_job",
        fake_enqueue,
    )
    monkeypatch.setattr(
        website_crawl_worker,
        "SessionLocal",
        TestingSessionLocal,
    )
    monkeypatch.setattr(
        website_crawl_worker,
        "crawl_website",
        fake_crawl_website,
    )

    response = client.post(
        f"/api/v1/companies/{created.json()['id']}/website-crawl"
    )

    assert response.status_code == 202
    asyncio.run(website_crawl_worker.process_job(UUID(response.json()["id"])))
    assert crawled_urls == ["https://example.com/"]


def test_company_research_source_endpoint_is_removed(client):
    response = client.post(
        f"/api/v1/companies/{UUID(int=1)}/sources/crawl",
        json={"source_url": "https://example.com"},
    )
    assert response.status_code == 404
