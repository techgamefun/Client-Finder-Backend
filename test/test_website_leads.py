from uuid import uuid4

from sqlalchemy import select

from conftest import TestingSessionLocal
from app.models import Company, Contact, Website, WebsiteCrawl


def test_add_filtered_websites_to_leads_creates_companies_and_skips_ineligible(
    client,
):
    with TestingSessionLocal() as db:
        new_crawl = WebsiteCrawl(
            website_url="https://new.example/",
            emails=["hello@new.example", "HELLO@new.example"],
            phones=["+1 (555) 0100", "15550100"],
        )
        existing_crawl = WebsiteCrawl(
            website_url="https://existing.example/",
            emails=["sales@existing.example"],
            phones=["+1 555 0200"],
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
        existing_company = Company(
            name="Existing Company",
            website="https://existing.example/",
            domain="existing.example",
        )
        existing_company.contacts.append(
            Contact(
                name="Sales Team",
                business_email="SALES@EXISTING.EXAMPLE",
                is_publicly_listed=False,
            )
        )
        db.add_all(
            [
                new_website,
                existing_website,
                listing_website,
                uncrawled_website,
                existing_company,
            ]
        )
        db.commit()
        existing_company_id = existing_company.id
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
        contacts = db.scalars(select(Contact)).all()
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
    new_contacts = [
        contact for contact in contacts if contact.company_id == created_company.id
    ]
    assert sorted(contact.business_email for contact in new_contacts if contact.business_email) == [
        "hello@new.example",
    ]
    assert sorted(contact.business_phone for contact in new_contacts if contact.business_phone) == [
        "+1 (555) 0100",
    ]
    existing_contacts = [
        contact
        for contact in contacts
        if contact.company_id == existing_company_id
    ]
    assert len(existing_contacts) == 2
    assert next(
        contact for contact in existing_contacts if contact.name == "Sales Team"
    ).is_publicly_listed is False
    assert sum(contact.business_phone == "+1 555 0200" for contact in existing_contacts) == 1
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
    with TestingSessionLocal() as db:
        repeated_company = db.scalar(
            select(Company).where(Company.domain == "new.example")
        )
        assert repeated_company is not None
        assert len(repeated_company.contacts) == 2


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


def test_batch_website_status_update_changes_selected_rows(client):
    with TestingSessionLocal() as db:
        websites = [
            Website(
                website_url=f"https://batch-{index}.example/",
                domain=f"batch-{index}.example",
            )
            for index in range(2)
        ]
        db.add_all(websites)
        db.commit()
        website_ids = [website.id for website in websites]

    response = client.patch(
        "/api/v1/websites/batch-status",
        json={
            "website_ids": [
                str(website_ids[0]),
                str(website_ids[0]),
                str(website_ids[1]),
                str(uuid4()),
            ],
            "status": "checked",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "requested_count": 3,
        "updated_count": 2,
        "not_found_count": 1,
    }

    with TestingSessionLocal() as db:
        assert all(
            db.get(Website, website_id).status == "checked"
            for website_id in website_ids
        )

    repeated = client.patch(
        "/api/v1/websites/batch-status",
        json={
            "website_ids": [str(website_id) for website_id in website_ids],
            "status": "checked",
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["updated_count"] == 0

    invalid_status = client.patch(
        "/api/v1/websites/batch-status",
        json={
            "website_ids": [str(website_ids[0])],
            "status": "ready",
        },
    )
    oversized_selection = client.patch(
        "/api/v1/websites/batch-status",
        json={
            "website_ids": [str(website_ids[0])] * 501,
            "status": "new",
        },
    )
    assert invalid_status.status_code == 422
    assert oversized_selection.status_code == 422
