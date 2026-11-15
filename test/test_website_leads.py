from uuid import uuid4

from sqlalchemy import select

from conftest import TestingSessionLocal
from app.models import Company, Website, WebsiteCrawl


def test_add_filtered_websites_to_leads_creates_companies_and_skips_ineligible(
    client,
):
    with TestingSessionLocal() as db:
        new_crawl = WebsiteCrawl(website_url="https://new.example/")
        existing_crawl = WebsiteCrawl(
            website_url="https://existing.example/",
        )
        listing_crawl = WebsiteCrawl(
            website_url="https://directory.example/",
        )
        db.add_all([new_crawl, existing_crawl, listing_crawl])
        db.flush()

        new_website = Website(
            website_url=new_crawl.website_url,
            domain="new.example",
            crawl_id=new_crawl.id,
        )
        existing_website = Website(
            website_url=existing_crawl.website_url,
            domain="existing.example",
            crawl_id=existing_crawl.id,
            status="checked",
        )
        listing_website = Website(
            website_url=listing_crawl.website_url,
            domain="directory.example",
            crawl_id=listing_crawl.id,
            page_type="business_listing",
        )
        uncrawled_website = Website(
            website_url="https://uncrawled.example/",
            domain="uncrawled.example",
        )
        db.add_all(
            [
                new_website,
                existing_website,
                listing_website,
                uncrawled_website,
                Company(
                    name="Existing Company",
                    website="https://existing.example/",
                    domain="existing.example",
                ),
            ]
        )
        db.commit()
        website_ids = [
            new_website.id,
            existing_website.id,
            listing_website.id,
            uncrawled_website.id,
            uuid4(),
            new_website.id,
        ]
        new_website_id = new_website.id
        existing_website_id = existing_website.id
        listing_website_id = listing_website.id
        uncrawled_website_id = uncrawled_website.id

    response = client.post(
        "/api/v1/websites/add-to-leads",
        json={"website_ids": [str(website_id) for website_id in website_ids]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "requested_count": 5,
        "created_count": 1,
        "already_existing_count": 1,
        "status_updated_count": 2,
        "skipped_listing_count": 1,
        "skipped_uncrawled_count": 1,
        "not_found_count": 1,
    }

    with TestingSessionLocal() as db:
        companies = db.scalars(select(Company)).all()
        websites = {
            website.id: website.status
            for website in db.scalars(select(Website)).all()
        }

    created_company = next(
        company for company in companies if company.domain == "new.example"
    )
    assert created_company.name == "new.example"
    assert created_company.website == "https://new.example/"
    assert created_company.lead_status == "new"
    assert websites[new_website_id] == "lead"
    assert websites[existing_website_id] == "lead"
    assert websites[listing_website_id] == "new"
    assert websites[uncrawled_website_id] == "new"

    repeated_response = client.post(
        "/api/v1/websites/add-to-leads",
        json={"website_ids": [str(new_website_id)]},
    )
    assert repeated_response.status_code == 200
    assert repeated_response.json()["created_count"] == 0
    assert repeated_response.json()["already_existing_count"] == 1


def test_website_status_can_be_updated_to_checked(client):
    with TestingSessionLocal() as db:
        website = Website(
            website_url="https://checked.example/",
            domain="checked.example",
        )
        db.add(website)
        db.commit()
        website_id = website.id

    response = client.patch(
        f"/api/v1/websites/{website_id}",
        json={"status": "checked"},
    )
    invalid_response = client.patch(
        f"/api/v1/websites/{website_id}",
        json={"status": "ready"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "checked"
    assert invalid_response.status_code == 422
