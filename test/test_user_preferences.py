import json as json_module
from uuid import UUID, uuid4

from app.api import user_preferences as user_preferences_api
from app.config import settings
from app.main import app
from app.models import User, UserPreferences
from app.services import user_preferences_cache
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


def test_user_preferences_read_from_cache_before_database(client, monkeypatch):
    cached = {
        "theme": "dark",
        "sidebar_collapsed": True,
        "search": {"providers": None, "tavily": None},
    }
    monkeypatch.setattr(
        user_preferences_api,
        "read_cached_preferences",
        lambda _organization_id, _user_id: cached,
    )
    monkeypatch.setattr(
        user_preferences_api,
        "_get_preferences_row",
        lambda *_args: None,
    )

    response = client.get("/api/v1/preferences")

    assert response.status_code == 200
    assert response.json() == cached


def test_user_preferences_cache_is_populated_and_invalidated(
    client,
    monkeypatch,
):
    cache_writes = []
    cache_invalidations = []
    monkeypatch.setattr(
        user_preferences_api,
        "read_cached_preferences",
        lambda _organization_id, _user_id: None,
    )
    monkeypatch.setattr(
        user_preferences_api,
        "write_cached_preferences",
        lambda organization_id, user_id, preferences: cache_writes.append(
            (organization_id, user_id, preferences)
        ),
    )
    monkeypatch.setattr(
        user_preferences_api,
        "invalidate_cached_preferences",
        lambda organization_id, user_id: cache_invalidations.append(
            (organization_id, user_id)
        ),
    )

    initial = client.get("/api/v1/preferences")
    updated = client.patch(
        "/api/v1/preferences",
        json={"theme": "dark"},
    )

    assert initial.status_code == 200
    assert cache_writes[0][2] == initial.json()
    assert updated.status_code == 200
    assert len(cache_invalidations) == 1
    assert cache_invalidations[0] == cache_writes[0][:2]


def test_redis_preferences_cache_uses_user_scoped_ttl_commands(monkeypatch):
    organization_id = uuid4()
    user_id = uuid4()
    cached = {
        "theme": "dark",
        "sidebar_collapsed": True,
        "search": {"providers": None, "tavily": None},
    }
    commands = []

    class FakeResponse:
        def __init__(self, result):
            self.result = result

        def raise_for_status(self):
            return None

        def json(self):
            return {"result": self.result}

    class FakeClient:
        def __init__(self, *, timeout):
            assert timeout == 1.0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, *, headers, json):
            assert headers["Authorization"] == "Bearer test-token"
            commands.append(json)
            if json[0] == "GET":
                return FakeResponse(json_module.dumps(cached))
            return FakeResponse("OK")

    monkeypatch.setattr(settings, "upstash_redis_rest_url", "https://redis.test")
    monkeypatch.setattr(settings, "upstash_redis_rest_token", "test-token")
    monkeypatch.setattr(user_preferences_cache.httpx, "Client", FakeClient)

    assert (
        user_preferences_cache.read_cached_preferences(
            organization_id,
            user_id,
        )
        == cached
    )
    user_preferences_cache.write_cached_preferences(
        organization_id,
        user_id,
        cached,
    )
    user_preferences_cache.invalidate_cached_preferences(
        organization_id,
        user_id,
    )

    cache_key = f"user-preferences:v1:{organization_id}:{user_id}"
    assert commands[0] == ["GET", cache_key]
    assert commands[1] == [
        "SET",
        cache_key,
        json_module.dumps(cached, separators=(",", ":")),
        "EX",
        "180",
    ]
    assert commands[2] == ["DEL", cache_key]
