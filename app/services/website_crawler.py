import asyncio
import ipaddress
import math
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from fastapi import HTTPException, status
from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.models import WebsiteCrawl


MAX_PAGES = 4
MAX_PAGE_BYTES = 1_000_000
MAX_CONTENT_LENGTH = 20_000
MAX_REDIRECTS = 5
MAX_RETRIES = 2
MAX_RETRY_DELAY = 5.0
MAX_BROWSER_REQUESTS = 80
BLOCKED_RESOURCE_TYPES = {"font", "image", "media"}
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
REQUEST_TIMEOUT = 10.0
EMAIL_PATTERN = re.compile(
    r"(?i)\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+"
    r"(?:\.[A-Z0-9-]+)+\b"
)
PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9\s().-]{5,}[0-9]$")
SOCIAL_DOMAINS = (
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "pinterest.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "youtube.com",
)
CATEGORY_TERMS = {
    "about": ("about", "who-we-are", "our-story", "company"),
    "contact": ("contact", "get-in-touch", "reach-us"),
    "goals": ("mission", "vision", "purpose", "goal", "values", "objectives"),
}
BLOCK_TAGS = {"script", "style", "noscript", "svg"}
TEXT_BLOCK_TAGS = {
    "address",
    "article",
    "br",
    "div",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "header",
    "li",
    "main",
    "p",
    "section",
}


@dataclass(frozen=True)
class CrawlData:
    website_url: str
    about_page_url: str | None
    about_content: str | None
    contact_page_url: str | None
    contact_content: str | None
    goals_page_url: str | None
    goals_content: str | None
    emails: list[str]
    social_links: list[str]
    phones: list[str] = field(default_factory=list)
    company_links: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class PageData:
    url: str
    content: str
    links: list[tuple[str, str]]
    emails: list[str]
    social_links: list[str]
    phones: list[str]


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._blocked_depth = 0
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag in BLOCK_TAGS:
            self._blocked_depth += 1
            return
        if self._blocked_depth:
            return
        if tag in TEXT_BLOCK_TAGS:
            self.text_parts.append("\n")
        if tag == "a" and attributes.get("href"):
            self._anchor_href = attributes["href"]
            self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in BLOCK_TAGS and self._blocked_depth:
            self._blocked_depth -= 1
            return
        if self._blocked_depth:
            return
        if tag in TEXT_BLOCK_TAGS:
            self.text_parts.append("\n")
        if tag == "a" and self._anchor_href:
            self.links.append(
                (self._anchor_href, " ".join(self._anchor_text))
            )
            self._anchor_href = None
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._blocked_depth:
            return
        self.text_parts.append(data)
        if self._anchor_href:
            self._anchor_text.append(data)


def _normalize_website_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A public HTTP or HTTPS website URL is required.",
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The website URL contains an invalid port.",
        ) from exc

    expected_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != expected_port:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only standard HTTP and HTTPS ports are supported.",
        )

    host = parsed.hostname.lower()
    netloc = host
    if ":" in host:
        netloc = f"[{host}]"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))


def _assert_public_host(host: str) -> None:
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Private and local website addresses cannot be crawled.",
            )
        try:
            addresses = [
                ipaddress.ip_address(info[4][0])
                for info in socket.getaddrinfo(
                    host,
                    None,
                    type=socket.SOCK_STREAM,
                )
            ]
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The website hostname could not be resolved.",
            ) from exc

    if not addresses or any(not address.is_global for address in addresses):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Private and non-public website addresses cannot be crawled.",
        )


async def _validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only public HTTP or HTTPS website URLs can be crawled.",
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The website URL contains an invalid port.",
        ) from exc
    expected_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != expected_port:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only standard HTTP and HTTPS ports are supported.",
        )
    await asyncio.to_thread(_assert_public_host, parsed.hostname)


def _parse_html(url: str, html: str) -> PageData:
    parser = _PageParser()
    parser.feed(html)
    text = " ".join(" ".join(parser.text_parts).split())[:MAX_CONTENT_LENGTH]

    emails = set(EMAIL_PATTERN.findall(text))
    social_links: set[str] = set()
    phones: set[str] = set()
    links: list[tuple[str, str]] = []
    for href, label in parser.links:
        if href.lower().startswith("mailto:"):
            emails.update(EMAIL_PATTERN.findall(href[7:]))
            continue
        if href.lower().startswith("tel:"):
            phone = re.sub(r"[^\d+]", "", href[4:])
            if PHONE_PATTERN.fullmatch(phone):
                phones.add(phone)
            continue
        absolute_url = urljoin(url, href)
        parsed = urlsplit(absolute_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        clean_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
        )
        host = parsed.hostname.lower()
        if any(
            host == domain or host.endswith(f".{domain}")
            for domain in SOCIAL_DOMAINS
        ):
            social_links.add(clean_url)
        links.append((clean_url, label))

    return PageData(
        url=url,
        content=text,
        links=links,
        emails=sorted(email.lower() for email in emails),
        social_links=sorted(social_links),
        phones=sorted(phones),
    )


def _same_site(candidate: str, site_host: str) -> bool:
    candidate_host = (urlsplit(candidate).hostname or "").lower()
    normalized_candidate = candidate_host.removeprefix("www.")
    normalized_site = site_host.lower().removeprefix("www.")
    return normalized_candidate == normalized_site


def _display_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _page_error(url: str, reason: str) -> str:
    return f"Crawler could not fetch {_display_url(url)}: {reason}"


def _retry_delay(retry_after: str | None, attempt: int) -> float | None:
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError, OverflowError):
                retry_at = None
            if retry_at is None:
                return min(0.5 * (2**attempt), MAX_RETRY_DELAY)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
        if not math.isfinite(delay):
            return None
        if delay < 0:
            return 0
        if delay > MAX_RETRY_DELAY:
            return None
        return delay
    return min(0.5 * (2**attempt), MAX_RETRY_DELAY)


def _category_candidates(
    links: list[tuple[str, str]],
    site_host: str,
) -> dict[str, str]:
    candidates: dict[str, list[tuple[int, str]]] = {
        category: [] for category in CATEGORY_TERMS
    }
    for url, label in links:
        if not _same_site(url, site_host):
            continue
        path = urlsplit(url).path.lower()
        searchable = f"{path} {label.lower()}"
        for category, terms in CATEGORY_TERMS.items():
            score = sum(term in searchable for term in terms)
            if score:
                candidates[category].append((score, url))

    return {
        category: max(matches, key=lambda match: match[0])[1]
        for category, matches in candidates.items()
        if matches
    }


async def _fetch_page(
    context: BrowserContext,
    url: str,
    site_host: str,
) -> PageData | None:
    try:
        await _validate_public_url(url)
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=_page_error(url, str(exc.detail)),
        ) from exc

    request_count = 0
    blocked_navigation: str | None = None

    async def guard_request(route: Route) -> None:
        nonlocal request_count, blocked_navigation
        request = route.request
        request_count += 1
        parsed_request_url = urlsplit(request.url)
        if request_count > MAX_BROWSER_REQUESTS:
            await route.abort()
            return
        if parsed_request_url.scheme not in {"http", "https"}:
            await route.abort()
            return
        if request.is_navigation_request():
            if not _same_site(request.url, site_host):
                blocked_navigation = request.url
                await route.abort()
                return
            redirect_count = 0
            redirected_from = request.redirected_from
            while redirected_from is not None:
                redirect_count += 1
                redirected_from = redirected_from.redirected_from
            if redirect_count > MAX_REDIRECTS:
                blocked_navigation = request.url
                await route.abort()
                return
        if request.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return
        try:
            await _validate_public_url(request.url)
        except HTTPException:
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", guard_request)
    page = None
    try:
        page = await context.new_page()
        for request_attempt in range(MAX_RETRIES + 1):
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(REQUEST_TIMEOUT * 1000),
                )
            except PlaywrightTimeoutError as exc:
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail=_page_error(
                        url,
                        f"the browser request timed out after "
                        f"{REQUEST_TIMEOUT:g} seconds.",
                    ),
                ) from exc
            except PlaywrightError as exc:
                if blocked_navigation:
                    reason = (
                        "the page attempted to navigate to another website, "
                        "which the crawler does not follow."
                    )
                else:
                    reason = (
                        "the browser request failed "
                        f"({type(exc).__name__})."
                    )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=_page_error(url, reason),
                ) from exc

            if response is None:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=_page_error(
                        url,
                        "the browser did not receive a page response.",
                    ),
                )
            response_status = response.status
            if (
                response_status in RETRYABLE_STATUS_CODES
                and request_attempt < MAX_RETRIES
            ):
                delay = _retry_delay(
                    response.headers.get("retry-after"),
                    request_attempt,
                )
                if delay is not None:
                    await asyncio.sleep(delay)
                    continue
            if response_status == status.HTTP_404_NOT_FOUND:
                return None
            if response_status >= 400:
                if response_status == status.HTTP_403_FORBIDDEN:
                    reason = (
                        "the site refused the browser request "
                        "(HTTP 403 Forbidden). Confirm that this server is "
                        "authorized and allowlisted by the site owner."
                    )
                else:
                    status_text = response.status_text
                    status_label = (
                        f" {status_text}" if status_text else ""
                    )
                    reason = (
                        f"the server returned HTTP {response_status}"
                        f"{status_label}."
                    )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=_page_error(url, reason),
                )
            content_type = response.headers.get("content-type", "").lower()
            if (
                "text/html" not in content_type
                and "application/xhtml+xml" not in content_type
            ):
                return None

            html = await page.content()
            if len(html.encode("utf-8")) > MAX_PAGE_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=_page_error(
                        url,
                        f"the rendered page exceeds {MAX_PAGE_BYTES} bytes.",
                    ),
                )
            final_url = page.url
            if not _same_site(final_url, site_host):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=_page_error(
                        final_url,
                        "the page redirected to another website, which the "
                        "crawler does not follow.",
                    ),
                )
            try:
                await _validate_public_url(final_url)
            except HTTPException as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail=_page_error(final_url, str(exc.detail)),
                ) from exc
            return _parse_html(final_url, html)

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_page_error(
                url,
                "the server remained unavailable after "
                f"{MAX_RETRIES} retries.",
            ),
        )
    finally:
        if page is not None:
            await page.close()
        await context.unroute("**/*", guard_request)


async def crawl_website(website_url: str) -> CrawlData:
    canonical_url = _normalize_website_url(website_url)
    await _validate_public_url(canonical_url)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                context = await browser.new_context(service_workers="block")
                try:
                    homepage = await _fetch_page(
                        context,
                        canonical_url,
                        urlsplit(canonical_url).hostname or "",
                    )
                    if homepage is None:
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=_page_error(
                                canonical_url,
                                "the homepage returned no HTML (it may be "
                                "missing or serve a non-HTML response).",
                            ),
                        )

                    pages = {homepage.url: homepage}
                    candidates = _category_candidates(
                        homepage.links,
                        urlsplit(homepage.url).hostname or "",
                    )
                    for category, page_url in candidates.items():
                        if page_url in pages or len(pages) >= MAX_PAGES:
                            continue
                        page = await _fetch_page(
                            context,
                            page_url,
                            urlsplit(homepage.url).hostname or "",
                        )
                        if page is not None:
                            pages[page.url] = page
                            candidates[category] = page.url
                finally:
                    await context.close()
            finally:
                await browser.close()
    except PlaywrightError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The browser crawler could not start Chromium. Install the "
                "Playwright browser with `python -m playwright install "
                "chromium` on the backend host."
            ),
        ) from exc

    category_content: dict[str, tuple[str, str] | None] = {
        "about": None,
        "contact": None,
        "goals": None,
    }
    for category, page_url in candidates.items():
        page = pages.get(page_url)
        if page:
            category_content[category] = (page.url, page.content)
    if category_content["goals"] is None and category_content["about"] is not None:
        about_url, about_content = category_content["about"]
        goal_paragraphs = [
            paragraph
            for paragraph in re.split(r"(?<=[.!?])\s+", about_content)
            if re.search(
                r"\b(mission|vision|goal|purpose|objective|values)\b",
                paragraph,
                re.I,
            )
        ]
        if goal_paragraphs:
            category_content["goals"] = (
                about_url,
                " ".join(goal_paragraphs)[:MAX_CONTENT_LENGTH],
            )

    emails = sorted({email for page in pages.values() for email in page.emails})
    social_links = sorted(
        {link for page in pages.values() for link in page.social_links}
    )
    phones = sorted({phone for page in pages.values() for phone in page.phones})
    company_links = sorted(
        {
            link
            for page in pages.values()
            for link in page.links
            if not any(
                (urlsplit(link[0]).hostname or "").lower() == domain
                or (urlsplit(link[0]).hostname or "")
                .lower()
                .endswith(f".{domain}")
                for domain in SOCIAL_DOMAINS
            )
        }
    )
    about = category_content["about"]
    contact = category_content["contact"]
    goals = category_content["goals"]
    return CrawlData(
        website_url=canonical_url,
        about_page_url=about[0] if about else None,
        about_content=about[1] if about else None,
        contact_page_url=contact[0] if contact else None,
        contact_content=contact[1] if contact else None,
        goals_page_url=goals[0] if goals else None,
        goals_content=goals[1] if goals else None,
        emails=emails,
        social_links=social_links,
        phones=phones,
        company_links=company_links,
    )


def _persist_crawl(data: CrawlData, db: Session) -> WebsiteCrawl:
    for attempt in range(2):
        try:
            crawl = db.scalar(
                select(WebsiteCrawl)
                .where(WebsiteCrawl.website_url == data.website_url)
                .limit(1)
            )
            if crawl is None:
                crawl = WebsiteCrawl(website_url=data.website_url)
                db.add(crawl)

            crawl.about_page_url = data.about_page_url
            crawl.about_content = data.about_content
            crawl.contact_page_url = data.contact_page_url
            crawl.contact_content = data.contact_content
            crawl.goals_page_url = data.goals_page_url
            crawl.goals_content = data.goals_content
            crawl.emails = data.emails
            crawl.social_links = data.social_links
            crawl.crawled_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(crawl)
            return crawl
        except IntegrityError:
            db.rollback()
            if attempt == 1:
                raise
        except Exception:
            db.rollback()
            raise

    raise RuntimeError("Could not persist website crawl after retry.")


def get_website_crawl(website_url: str, db: Session) -> WebsiteCrawl | None:
    canonical_url = _normalize_website_url(website_url)
    return db.scalar(
        select(WebsiteCrawl).where(WebsiteCrawl.website_url == canonical_url)
    )


async def crawl_and_persist(
    website_url: str,
    db: Session,
) -> WebsiteCrawl:
    crawl_data = await crawl_website(website_url)
    return await run_in_threadpool(_persist_crawl, crawl_data, db)
