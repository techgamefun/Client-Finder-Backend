from uuid import UUID, uuid4

from app.main import app
from app.models import User, UserPreferences
from conftest import TestingSessionLocal


def _session_ids(client):
    session = client.get("/api/v1/auth/me").json()
    return UUID(session["organization_id"]), UUID(session["user"]["id"])


def test_user_preferences_save_and_merge_per_user(client):
    initial = client.get("/api/v1/preferences")
    assert initial.status_code == 200
    assert initial.json() == {
        "theme": "system",
        "sidebar_collapsed": False,
        "search": {"providers": None, "tavily": None},
    }

    updated = client.patch(
        "/api/v1/preferences",
        json={
            "theme": "dark",
            "sidebar_collapsed": True,
            "search": {
                "providers": ["tavily"],
                "tavily": {
                    "topic": "news",
                    "search_depth": "advanced",
                    "max_results": 10,
                    "country": "canada",
                    "exclude_domains": ["example.com"],
                    "date_mode": "relative",
                    "time_range": "week",
                    "filter_by_published_date": True,
                    "min_score": 0.4,
                    "include_answer": True,
                    "auto_parameters": True,
                    "auto_find_more": True,
                },
            },
        },
    )
    assert updated.status_code == 200
    assert updated.json()["theme"] == "dark"
    assert updated.json()["sidebar_collapsed"] is True
    assert updated.json()["search"]["providers"] == ["tavily"]

    partial_update = client.patch(
        "/api/v1/preferences",
        json={"search": {"tavily": {"exclude_domains": []}}},
    )
    assert partial_update.status_code == 200
    assert partial_update.json()["theme"] == "dark"
    assert partial_update.json()["sidebar_collapsed"] is True
    assert partial_update.json()["search"]["tavily"]["topic"] == "news"
    assert partial_update.json()["search"]["tavily"]["exclude_domains"] == []

    persisted = client.get("/api/v1/preferences")
    assert persisted.status_code == 200
    assert persisted.json() == partial_update.json()


def test_user_preferences_are_isolated_between_users(client):
    organization_id, user_id = _session_ids(client)
    other_user_id = uuid4()
    with TestingSessionLocal() as db:
        db.add(
            User(
                id=other_user_id,
                organization_id=organization_id,
                email="another-preferences-user@example.test",
                password_hash="unused-test-hash",
                role="user",
            )
        )
        db.add(
            UserPreferences(
                user_id=other_user_id,
                organization_id=organization_id,
                preferences={
                    "theme": "dark",
                    "sidebar_collapsed": True,
                },
            )
        )
        db.commit()

    response = client.get("/api/v1/preferences")

    assert response.status_code == 200
    assert response.json()["theme"] == "system"
    assert response.json()["sidebar_collapsed"] is False
    with TestingSessionLocal() as db:
        own_preferences = db.get(UserPreferences, user_id)
        assert own_preferences is None


def test_user_preferences_validate_values_and_require_authentication(client):
    invalid = client.patch(
        "/api/v1/preferences",
        json={"theme": "blue"},
    )
    assert invalid.status_code == 422
    invalid_country = client.patch(
        "/api/v1/preferences",
        json={"search": {"tavily": {"country": "Canada"}}},
    )
    assert invalid_country.status_code == 422

    client.cookies.clear()
    unauthenticated = client.get("/api/v1/preferences")
    assert unauthenticated.status_code == 401
