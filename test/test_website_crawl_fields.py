from sqlalchemy import select

from app.models import Website, WebsiteCrawl
from app.schemas.website_crawl import WebsiteCrawlResponse
from app.services.website_crawler import CrawlData, _persist_crawl
from conftest import TestingSessionLocal


def test_crawl_data_parses_site_type_from_crawler_response():
    data = CrawlData.model_validate(
        {
            "website_url": "https://example.com/",
            "site_type": "news_editorial",
            "website_topics": ["real estate", "property management"],
            "language": "fr-CA",
            "about_page_url": None,
            "about_content": None,
            "contact_page_url": None,
            "contact_content": None,
            "goals_page_url": None,
            "goals_content": None,
        }
    )

    assert data.site_type == "news_editorial"
    assert data.website_topics == ["real estate", "property management"]
    assert data.language == "fr-CA"


def test_persist_crawl_stores_home_blog_and_service_pages(client):
    data = CrawlData(
        website_url="https://example.com/",
        site_type="news_editorial",
        website_topics=["real estate", "property management"],
        language="fr-CA",
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
        website = db.scalar(
            select(Website).where(Website.website_url == data.website_url)
        )

    assert response.home_page_url == "https://example.com/"
    assert response.site_type == "news_editorial"
    assert response.home_content == "Welcome to Example."
    assert response.blog_page_url == "https://example.com/blog"
    assert response.blog_content == "Company updates."
    assert response.service_page_url == "https://example.com/services"
    assert response.service_content == "Our services."
    assert website is not None
    assert website.language == "fr-CA"
    assert website.site_topic == ["real estate", "property management"]


def test_persist_crawl_does_not_replace_existing_site_topics(client):
    with TestingSessionLocal() as db:
        db.add(
            Website(
                website_url="https://example.com/",
                domain="example.com",
                site_topic=["manually reviewed topic"],
            )
        )
        db.commit()
        data = CrawlData(
            website_url="https://example.com/",
            site_type="news_editorial",
            website_topics=["automatically inferred topic"],
            about_page_url=None,
            about_content=None,
            contact_page_url=None,
            contact_content=None,
            goals_page_url=None,
            goals_content=None,
        )
        _persist_crawl(data, db)
        website = db.scalar(
            select(Website).where(Website.website_url == data.website_url)
        )

    assert website is not None
    assert website.site_topic == ["manually reviewed topic"]


def test_persist_crawl_stores_classification_signals(client):
    features = {
        "og_type": "website",
        "generator": "Shopify",
        "json_ld_scripts": ['{"@type":"Product","name":"Widget"}'],
        "json_ld_schema_types": ["product", "website"],
        "microdata_schema_types": ["product"],
        "heading_texts": ["Widget"],
        "first_paragraph_text": "The best widget.",
        "article_count": 2,
        "password_input_count": 1,
        "shopping_elements": {"product_schema": 1, "price_elements": 1},
        "nav_link_texts": ["Shop"],
        "footer_link_texts": ["Terms"],
        "header_link_texts": ["Log in"],
        "cta_texts": ["Add to Cart"],
        "url_path_patterns": ["/products"],
    }
    data = CrawlData.model_validate(
        {
            "website_url": "https://example.com/",
            "about_page_url": None,
            "about_content": None,
            "contact_page_url": None,
            "contact_content": None,
            "goals_page_url": None,
            "goals_content": None,
            "pages": [
                {
                    "url": "https://example.com/products/widget",
                    "rendering_mode": "http_only",
                    "title": "Widget",
                    "text": "Product details",
                    "classification_signals": features,
                }
            ],
        }
    )

    with TestingSessionLocal() as db:
        crawl = _persist_crawl(data, db)
        response = WebsiteCrawlResponse.model_validate(crawl)

    assert len(crawl.classification_signals) == 1
    assert crawl.classification_signals[0]["url"] == (
        "https://example.com/products/widget"
    )
    assert (
        crawl.classification_signals[0]["classification_signals"]["og_type"]
        == "website"
    )
    assert response.classification_signals[0].url == (
        "https://example.com/products/widget"
    )
    assert response.classification_signals[0].rendering_mode == "http_only"
    assert (
        response.classification_signals[0].classification_signals.og_type
        == "website"
    )
    assert (
        response.classification_signals[0]
        .classification_signals.shopping_elements["product_schema"]
        == 1
    )
    assert (
        response.classification_signals[0]
        .classification_signals.json_ld_scripts[0]
        == '{"@type":"Product","name":"Widget"}'
    )
    assert (
        response.classification_signals[0]
        .classification_signals.footer_link_texts
        == ["Terms"]
    )
    assert (
        response.classification_signals[0]
        .classification_signals.first_paragraph_text
        == "The best widget."
    )


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
