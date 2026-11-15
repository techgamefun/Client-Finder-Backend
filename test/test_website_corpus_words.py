from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from conftest import TestingSessionLocal
from app.models import Website, WebsiteCorpusAnalysis, WebsiteCrawl
from app.services import website_corpus_words


def _create_crawled_website(
    db,
    domain: str,
    title: str,
    *,
    crawled_at: datetime,
) -> Website:
    website_url = f"https://{domain}/"
    crawl = WebsiteCrawl(
        website_url=website_url,
        classification_signals=[
            {
                "url": website_url,
                "classification_signals": {
                    "page_title": title,
                    "meta_description": f"{title} services",
                },
            },
        ],
        crawled_at=crawled_at,
    )
    db.add(crawl)
    db.flush()
    website = Website(
        website_url=website_url,
        domain=domain,
        crawl_id=crawl.id,
        discovery_source="manual",
    )
    db.add(website)
    db.flush()
    return website


def _crawler_response(url: str, sites: list[dict[str, object]]) -> httpx.Response:
    results = []
    for site in sites:
        domain = site["domain"]
        keyword = (
            "residential plumbing"
            if domain == "example.com"
            else "organic garden"
        )
        results.append(
            {
                "domain": domain,
                "words": [
                    {
                        "word": keyword,
                        "score": 1.25,
                        "is_phrase": True,
                    },
                ],
            },
        )
    return httpx.Response(
        200,
        json={"sites": results},
        request=httpx.Request("POST", url),
    )


def test_corpus_words_endpoint_returns_empty_list_without_crawled_websites(
    client,
):
    response = client.get("/api/v1/websites/corpus-words")

    assert response.status_code == 200
    assert response.json() == {"websites": []}


def test_corpus_words_are_analyzed_and_cached_per_website(client, monkeypatch):
    crawled_at = datetime(2026, 11, 14, tzinfo=timezone.utc)
    with TestingSessionLocal() as db:
        first = _create_crawled_website(
            db,
            "example.com",
            "Residential plumbing",
            crawled_at=crawled_at,
        )
        second = _create_crawled_website(
            db,
            "garden.example",
            "Organic garden",
            crawled_at=crawled_at,
        )
        first_id = first.id
        second_id = second.id
        db.commit()

    calls = []

    def fake_post(url, *, json, timeout):
        calls.append(json)
        return _crawler_response(url, json["sites"])

    monkeypatch.setattr(website_corpus_words.httpx, "post", fake_post)
    first_response = client.get("/api/v1/websites/corpus-words")
    second_response = client.get("/api/v1/websites/corpus-words")

    assert first_response.status_code == 200
    analyses = {
        analysis["website_id"]: analysis
        for analysis in first_response.json()["websites"]
    }
    assert set(analyses) == {str(first_id), str(second_id)}
    assert analyses[str(first_id)]["domain"] == "example.com"
    assert analyses[str(first_id)]["words"] == [
        {
            "word": "residential plumbing",
            "score": 1.25,
            "is_phrase": True,
        },
    ]
    assert analyses[str(second_id)]["domain"] == "garden.example"
    assert analyses[str(second_id)]["words"] == [
        {
            "word": "organic garden",
            "score": 1.25,
            "is_phrase": True,
        },
    ]
    assert analyses[str(first_id)]["computed_at"]
    assert second_response.json() == first_response.json()
    assert len(calls) == 1
    assert calls[0]["sites"] == [
        {
            "domain": "example.com",
            "titles": ["Residential plumbing"],
            "descriptions": ["Residential plumbing services"],
        },
        {
            "domain": "garden.example",
            "titles": ["Organic garden"],
            "descriptions": ["Organic garden services"],
        },
    ]

    with TestingSessionLocal() as db:
        saved = {
            analysis.website_id: analysis
            for analysis in db.scalars(select(WebsiteCorpusAnalysis)).all()
        }
        assert set(saved) == {first_id, second_id}
        assert saved[first_id].algorithm_version != 0
        assert saved[second_id].algorithm_version != 0


def test_re_crawl_recomputes_only_that_website(client, monkeypatch):
    old_time = datetime(2026, 11, 13, tzinfo=timezone.utc)
    new_time = datetime(2026, 11, 14, tzinfo=timezone.utc)
    with TestingSessionLocal() as db:
        first = _create_crawled_website(
            db,
            "example.com",
            "Residential plumbing",
            crawled_at=old_time,
        )
        second = _create_crawled_website(
            db,
            "garden.example",
            "Organic garden",
            crawled_at=old_time,
        )
        first_id = first.id
        db.commit()

    calls = []

    def fake_post(url, *, json, timeout):
        calls.append(json)
        return _crawler_response(url, json["sites"])

    monkeypatch.setattr(website_corpus_words.httpx, "post", fake_post)
    assert client.get("/api/v1/websites/corpus-words").status_code == 200

    with TestingSessionLocal() as db:
        crawl = db.scalar(
            select(WebsiteCrawl).where(
                WebsiteCrawl.website_url == "https://example.com/",
            ),
        )
        assert crawl is not None
        crawl.classification_signals = [
            {
                "url": crawl.website_url,
                "classification_signals": {
                    "page_title": "Commercial heating",
                },
            },
        ]
        crawl.crawled_at = new_time
        db.commit()

    response = client.get("/api/v1/websites/corpus-words")
    assert response.status_code == 200
    assert len(calls) == 2
    assert calls[1]["sites"] == [
        {
            "domain": "example.com",
            "titles": ["Commercial heating"],
            "descriptions": [],
        },
    ]
    with TestingSessionLocal() as db:
        first_analysis = db.get(WebsiteCorpusAnalysis, first_id)
        second_website = db.scalar(
            select(Website).where(Website.domain == "garden.example"),
        )
        assert first_analysis is not None
        assert second_website is not None
        second_analysis = db.get(WebsiteCorpusAnalysis, second_website.id)
        assert second_analysis is not None
        assert first_analysis.source_crawled_at.replace(
            tzinfo=timezone.utc,
        ) == new_time
        assert second_analysis.source_crawled_at.replace(
            tzinfo=timezone.utc,
        ) == old_time


def test_corpus_words_recomputes_when_analyzer_version_changes(
    client,
    monkeypatch,
):
    crawled_at = datetime(2026, 11, 14, tzinfo=timezone.utc)
    with TestingSessionLocal() as db:
        website = _create_crawled_website(
            db,
            "example.com",
            "Old saved words",
            crawled_at=crawled_at,
        )
        db.add(
            WebsiteCorpusAnalysis(
                website_id=website.id,
                words=[
                    {
                        "word": "stale",
                        "score": 1.0,
                        "is_phrase": False,
                    },
                ],
                algorithm_version=1,
                source_crawled_at=crawled_at,
                computed_at=crawled_at,
            ),
        )
        website_id = website.id
        db.commit()

    calls = []

    def fake_post(url, *, json, timeout):
        calls.append(json)
        return _crawler_response(url, json["sites"])

    monkeypatch.setattr(website_corpus_words.httpx, "post", fake_post)
    response = client.get("/api/v1/websites/corpus-words")

    assert response.status_code == 200
    assert response.json()["websites"][0]["words"][0]["word"] == (
        "residential plumbing"
    )
    assert len(calls) == 1
    with TestingSessionLocal() as db:
        analysis = db.get(WebsiteCorpusAnalysis, website_id)
        assert analysis is not None
        assert analysis.algorithm_version != 1


def test_corpus_words_endpoint_reports_crawler_service_failures(
    client,
    monkeypatch,
):
    with TestingSessionLocal() as db:
        _create_crawled_website(
            db,
            "unavailable.example",
            "Business information",
            crawled_at=datetime(2026, 11, 14, tzinfo=timezone.utc),
        )
        db.commit()

    def fail_post(*_args, **_kwargs):
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(website_corpus_words.httpx, "post", fail_post)

    response = client.get("/api/v1/websites/corpus-words")

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]
    with TestingSessionLocal() as db:
        assert db.scalar(select(WebsiteCorpusAnalysis)) is None


def test_corpus_words_reports_outdated_crawler_response_contract(
    client,
    monkeypatch,
):
    with TestingSessionLocal() as db:
        _create_crawled_website(
            db,
            "outdated.example",
            "Local services",
            crawled_at=datetime(2026, 11, 14, tzinfo=timezone.utc),
        )
        db.commit()

    def fake_post(url, *, json, timeout):
        return httpx.Response(
            200,
            json={"words": []},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(website_corpus_words.httpx, "post", fake_post)
    response = client.get("/api/v1/websites/corpus-words")

    assert response.status_code == 502
    assert "Redeploy or restart crawler-service" in response.json()["detail"]
