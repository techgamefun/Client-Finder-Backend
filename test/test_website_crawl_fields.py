from app.models import WebsiteCrawl
from app.schemas.website_crawl import WebsiteCrawlResponse
from app.services.website_crawler import CrawlData, _persist_crawl
from conftest import TestingSessionLocal


def test_crawl_data_parses_site_type_from_crawler_response():
    data = CrawlData.model_validate(
        {
            "website_url": "https://example.com/",
            "site_type": "news_editorial",
            "about_page_url": None,
            "about_content": None,
            "contact_page_url": None,
            "contact_content": None,
            "goals_page_url": None,
            "goals_content": None,
        }
    )

    assert data.site_type == "news_editorial"


def test_persist_crawl_stores_home_blog_and_service_pages(client):
    data = CrawlData(
        website_url="https://example.com/",
        site_type="news_editorial",
        home_page_url="https://example.com/",
        home_content="Welcome to Example.",
        about_page_url=None,
        about_content=None,
        blog_page_url="https://example.com/blog",
        blog_content="Company updates.",
        service_page_url="https://example.com/services",
        service_content="Our services.",
        contact_page_url=None,
        contact_content=None,
        goals_page_url=None,
        goals_content=None,
    )

    with TestingSessionLocal() as db:
        crawl = _persist_crawl(data, db)
        response = WebsiteCrawlResponse.model_validate(crawl)

    assert response.home_page_url == "https://example.com/"
    assert response.site_type == "news_editorial"
    assert response.home_content == "Welcome to Example."
    assert response.blog_page_url == "https://example.com/blog"
    assert response.blog_content == "Company updates."
    assert response.service_page_url == "https://example.com/services"
    assert response.service_content == "Our services."


def test_persist_crawl_preserves_site_type_if_older_crawler_omits_it(client):
    data = CrawlData(
        website_url="https://example.com/",
        about_page_url=None,
        about_content=None,
        contact_page_url=None,
        contact_content=None,
        goals_page_url=None,
        goals_content=None,
    )

    with TestingSessionLocal() as db:
        crawl = WebsiteCrawl(
            website_url=data.website_url,
            site_type="news_editorial",
        )
        db.add(crawl)
        db.commit()

        updated = _persist_crawl(data, db)

    assert updated.site_type == "news_editorial"
