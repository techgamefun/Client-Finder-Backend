from app.schemas.website_crawl import WebsiteCrawlResponse
from app.services.website_crawler import CrawlData, _persist_crawl
from conftest import TestingSessionLocal


def test_persist_crawl_stores_home_blog_and_service_pages(client):
    data = CrawlData(
        website_url="https://example.com/",
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
    assert response.home_content == "Welcome to Example."
    assert response.blog_page_url == "https://example.com/blog"
    assert response.blog_content == "Company updates."
    assert response.service_page_url == "https://example.com/services"
    assert response.service_content == "Our services."
