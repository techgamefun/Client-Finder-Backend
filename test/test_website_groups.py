from uuid import UUID, uuid4

from sqlalchemy import select

from app.models import Website, WebsiteGroup
from conftest import TestingSessionLocal


def test_website_groups_support_creation_membership_update_and_delete(client):
    with TestingSessionLocal() as db:
        websites = [
            Website(
                website_url=f"https://group-{index}.example/",
                domain=f"group-{index}.example",
            )
            for index in range(2)
        ]
        db.add_all(websites)
        db.commit()
        website_ids = [website.id for website in websites]

    created = client.post(
        "/api/v1/websites/groups",
        json={
            "name": "  Priority accounts  ",
            "website_ids": [str(website_ids[0])],
        },
    )
    assert created.status_code == 201, created.text
    group_id = UUID(created.json()["id"])
    assert created.json()["name"] == "Priority accounts"
    assert created.json()["website_count"] == 1

    added = client.post(
        "/api/v1/websites/groups/members",
        json={
            "group_id": str(group_id),
            "website_ids": [str(website_ids[1])],
            "action": "add",
        },
    )
    assert added.status_code == 200, added.text
    assert added.json()["updated_count"] == 1

    renamed = client.patch(
        f"/api/v1/websites/groups/{group_id}",
        json={"name": "Qualified accounts"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Qualified accounts"
    assert renamed.json()["website_count"] == 2

    with TestingSessionLocal() as db:
        group = db.scalar(select(WebsiteGroup).where(WebsiteGroup.id == group_id))
        assert group is not None
        assert {website.id for website in group.websites} == set(website_ids)

    deleted = client.delete(f"/api/v1/websites/groups/{group_id}")
    assert deleted.status_code == 204
    assert client.get("/api/v1/websites/groups").json() == []


def test_website_contact_crud_is_scoped_to_website(client):
    with TestingSessionLocal() as db:
        websites = [
            Website(
                website_url=f"https://contact-{index}.example/",
                domain=f"contact-{index}.example",
            )
            for index in range(2)
        ]
        db.add_all(websites)
        db.commit()
        first_id, second_id = (website.id for website in websites)

    created = client.post(
        f"/api/v1/websites/{first_id}/contacts",
        json={
            "name": "Alex Example",
            "role": "Operations",
            "business_email": "alex@example.test",
            "is_publicly_listed": True,
        },
    )
    assert created.status_code == 201, created.text
    contact_id = created.json()["id"]
    assert created.json()["website_id"] == str(first_id)
    assert client.get(f"/api/v1/websites/{first_id}/contacts").json()[0]["id"] == contact_id

    updated = client.patch(
        f"/api/v1/websites/{first_id}/contacts/{contact_id}",
        json={"role": "Director"},
    )
    assert updated.status_code == 200
    assert updated.json()["role"] == "Director"

    cross_website = client.patch(
        f"/api/v1/websites/{second_id}/contacts/{contact_id}",
        json={"role": "Other"},
    )
    assert cross_website.status_code == 404

    deleted = client.delete(
        f"/api/v1/websites/{first_id}/contacts/{contact_id}"
    )
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/websites/{first_id}/contacts").json() == []
