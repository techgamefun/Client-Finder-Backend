from itertools import count

from app.config import settings
from app.services import website_crawler


def test_crawler_service_pool_round_robins_and_removes_health_path(monkeypatch):
    service_urls = [
        "https://webcrawler-i5cl.onrender.com",
        "https://webcrawler-3.onrender.com",
        "https://webcrawler-2.onrender.com",
        "https://webcrawler-1-3hus.onrender.com",
        "https://webcrawler-4.onrender.com",
    ]
    monkeypatch.setattr(
        settings,
        "crawler_service_urls",
        ",".join(f"{url}/health" for url in service_urls),
    )
    monkeypatch.setattr(website_crawler, "_crawler_service_index", count(0))

    assert [
        website_crawler._next_crawler_service_url()
        for _ in service_urls
    ] == service_urls


def test_crawler_service_pool_falls_back_to_single_url(monkeypatch):
    monkeypatch.setattr(settings, "crawler_service_urls", None)
    monkeypatch.setattr(
        settings,
        "crawler_service_url",
        "http://crawler.internal:8001/",
    )

    assert website_crawler._crawler_service_base_urls() == (
        "http://crawler.internal:8001",
    )
