from typing import Literal

from sqlalchemy import (
    JSON,
    String,
    case,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.orm import Session, contains_eager, joinedload, load_only

from app.models import Website, WebsiteCrawl, WebsiteHypeStatProfile

WebsiteSortKey = Literal[
    "website",
    "website_type",
    "site_topic",
    "language",
    "crawled_at",
    "hypestat_monthly_visits",
    "hypestat_referring_domains",
    "hypestat_global_rank",
    "hypestat_country",
    "hypestat_backlink_countries",
    "hypestat_audience_countries",
]


def _json_path(value, *path: str):
    for key in path:
        value = value[key]
    return value


def _profile_value(*path: str, as_integer: bool = False):
    payload = WebsiteHypeStatProfile.payload
    wrapped = _json_path(payload, "entry", "data", *path)
    direct = _json_path(payload, "data", *path)
    if as_integer:
        return func.coalesce(wrapped.as_integer(), direct.as_integer())
    return func.coalesce(wrapped.as_string(), direct.as_string())


def _profile_array(*path: str):
    payload = WebsiteHypeStatProfile.payload
    return func.coalesce(
        _json_path(payload, "entry", "data", *path),
        _json_path(payload, "data", *path),
    )


def _array_has_string(db: Session, array, value: str):
    if db.get_bind().dialect.name == "sqlite":
        elements = func.json_each(array).table_valued("key", "value")
        return exists(
            select(literal(1))
            .select_from(elements)
            .where(elements.c.value == value)
        )

    elements = func.json_array_elements_text(array).table_valued("value")
    return exists(
        select(literal(1))
        .select_from(elements)
        .where(elements.c.value == value)
    )


def _backlink_country_exists(db: Session, country: str):
    if db.get_bind().dialect.name == "sqlite":
        elements = func.json_each(
            _profile_array("backlinks", "by_country")
        ).table_valued("key", "value")
        country_value = func.json_extract(elements.c.value, "$.country")
    else:
        elements = func.json_array_elements(
            _profile_array("backlinks", "by_country")
        ).table_valued("value")
        country_value = cast(elements.c.value, JSON)["country"].as_string()
    return exists(
        select(literal(1))
        .select_from(elements)
        .where(country_value == country)
    )


def _audience_country_exists(db: Session, country: str):
    array = _profile_array("traffic", "countries")
    if db.get_bind().dialect.name == "sqlite":
        elements = func.json_each(array).table_valued("key", "value")
        country_value = func.coalesce(
            func.json_extract(elements.c.value, "$.country"),
            func.json_extract(elements.c.value, "$.name"),
            func.json_extract(elements.c.value, "$"),
        )
    else:
        elements = func.json_array_elements(array).table_valued("value")
        item = cast(elements.c.value, JSON)
        country_value = func.coalesce(
            item["country"].as_string(),
            item["name"].as_string(),
            func.replace(cast(elements.c.value, String), '"', ""),
        )
    return exists(
        select(literal(1))
        .select_from(elements)
        .where(country_value == country)
    )


def _conditions(
    db: Session,
    *,
    search: str,
    status: str | None,
    page_type: str | None,
    category: str | None,
    discovery_source: str | None,
    minimum_monthly_visits: float | None,
    minimum_referring_domains: float | None,
    maximum_global_rank: float | None,
    hypestat_country: str | None,
    backlink_country: str | None,
    audience_country: str | None,
):
    conditions = [Website.crawl_id.is_not(None)]
    if status:
        conditions.append(Website.status == status)
    if page_type:
        conditions.append(Website.page_type == page_type)
    if category == "unclassified":
        conditions.append(func.json_array_length(Website.site_topic) == 0)
    elif category:
        conditions.append(_array_has_string(db, Website.site_topic, category))
    if discovery_source:
        conditions.append(
            or_(
                Website.discovery_source == discovery_source,
                Website.discovery_provider == discovery_source,
            )
        )

    monthly_visits = _profile_value(
        "traffic",
        "visits_monthly",
        as_integer=True,
    )
    referring_domains = _profile_value(
        "backlinks",
        "referring_domains",
        as_integer=True,
    )
    global_rank = _profile_value("rank", "hypestat", as_integer=True)
    if minimum_monthly_visits is not None:
        conditions.append(monthly_visits >= minimum_monthly_visits)
    if minimum_referring_domains is not None:
        conditions.append(referring_domains >= minimum_referring_domains)
    if maximum_global_rank is not None:
        conditions.append(global_rank <= maximum_global_rank)
    if hypestat_country:
        conditions.append(
            _profile_value("rank", "country", "country")
            == hypestat_country
        )
    if backlink_country:
        conditions.append(_backlink_country_exists(db, backlink_country))
    if audience_country:
        conditions.append(_audience_country_exists(db, audience_country))

    normalized_search = search.strip().casefold()
    if normalized_search:
        escaped = (
            normalized_search.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        search_pattern = f"%{escaped}%"
        search_fields = (
            Website.website_url,
            Website.domain,
            Website.category,
            Website.page_type,
            cast(Website.site_topic, String),
            Website.language,
            Website.discovery_source,
            Website.discovery_provider,
            Website.status,
            cast(monthly_visits, String),
            cast(referring_domains, String),
            cast(global_rank, String),
            _profile_value("rank", "country", "country"),
            cast(_profile_array("backlinks", "by_country"), String),
            cast(_profile_array("traffic", "countries"), String),
        )
        conditions.append(
            or_(
                *(
                    func.lower(cast(field, String)).like(
                        search_pattern,
                        escape="\\",
                    )
                    for field in search_fields
                )
            )
        )
    return conditions


def get_website_registry_page(
    db: Session,
    *,
    limit: int,
    offset: int,
    search: str,
    status: str | None,
    page_type: str | None,
    category: str | None,
    discovery_source: str | None,
    minimum_monthly_visits: float | None,
    minimum_referring_domains: float | None,
    maximum_global_rank: float | None,
    hypestat_country: str | None,
    backlink_country: str | None,
    audience_country: str | None,
    sort_by: WebsiteSortKey | None,
    sort_direction: Literal["ascending", "descending"],
) -> tuple[list[Website], int, int, int]:
    conditions = _conditions(
        db,
        search=search,
        status=status,
        page_type=page_type,
        category=category,
        discovery_source=discovery_source,
        minimum_monthly_visits=minimum_monthly_visits,
        minimum_referring_domains=minimum_referring_domains,
        maximum_global_rank=maximum_global_rank,
        hypestat_country=hypestat_country,
        backlink_country=backlink_country,
        audience_country=audience_country,
    )
    has_filters = any(
        (
            search.strip(),
            status,
            page_type,
            category,
            discovery_source,
            minimum_monthly_visits is not None,
            minimum_referring_domains is not None,
            maximum_global_rank is not None,
            hypestat_country,
            backlink_country,
            audience_country,
        )
    )
    needs_profile_join = any(
        (
            search.strip(),
            minimum_monthly_visits is not None,
            minimum_referring_domains is not None,
            maximum_global_rank is not None,
            hypestat_country,
            backlink_country,
            audience_country,
        )
    )
    website_count, indexed_count = db.execute(
        select(
            func.count(Website.id),
            func.sum(
                case(
                    (Website.crawl_id.is_not(None), 1),
                    else_=0,
                )
            ),
        )
    ).one()
    website_count = website_count or 0
    indexed_count = indexed_count or 0
    if has_filters:
        count_query = select(func.count(Website.id)).where(*conditions)
        if needs_profile_join:
            count_query = count_query.outerjoin(
                WebsiteHypeStatProfile,
                WebsiteHypeStatProfile.website_id == Website.id,
            )
        total_count = db.scalar(count_query) or 0
    else:
        total_count = indexed_count
    last_offset = max(0, ((total_count - 1) // limit) * limit)
    offset = min(offset, last_offset)

    sort_columns = {
        "website": Website.domain,
        "website_type": Website.page_type,
        "site_topic": cast(Website.site_topic, String),
        "language": Website.language,
        "crawled_at": WebsiteCrawl.crawled_at,
        "hypestat_monthly_visits": _profile_value(
            "traffic",
            "visits_monthly",
            as_integer=True,
        ),
        "hypestat_referring_domains": _profile_value(
            "backlinks",
            "referring_domains",
            as_integer=True,
        ),
        "hypestat_global_rank": _profile_value(
            "rank",
            "hypestat",
            as_integer=True,
        ),
        "hypestat_country": _profile_value("rank", "country", "country"),
        "hypestat_backlink_countries": cast(
            _profile_array("backlinks", "by_country"),
            String,
        ),
        "hypestat_audience_countries": cast(
            _profile_array("traffic", "countries"),
            String,
        ),
    }
    if sort_by is None:
        ordering = [Website.created_at.desc(), Website.id.desc()]
    else:
        column = sort_columns[sort_by]
        sort_column = (
            column.asc()
            if sort_direction == "ascending"
            else column.desc()
        )
        ordering = [sort_column.nulls_last(), Website.id.desc()]

    page_query = select(Website)
    if sort_by == "crawled_at":
        page_query = (
            page_query.outerjoin(
                WebsiteCrawl,
                Website.crawl_id == WebsiteCrawl.id,
            )
            .options(
                contains_eager(Website.crawl).load_only(
                    WebsiteCrawl.crawled_at,
                ),
            )
        )
    else:
        page_query = page_query.options(
            joinedload(Website.crawl).load_only(WebsiteCrawl.crawled_at),
        )
    page_query = page_query.options(
        joinedload(Website.hypestat_profile).load_only(
            WebsiteHypeStatProfile.payload,
        ),
    )
    if needs_profile_join or sort_by in {
        "hypestat_monthly_visits",
        "hypestat_referring_domains",
        "hypestat_global_rank",
        "hypestat_country",
        "hypestat_backlink_countries",
        "hypestat_audience_countries",
    }:
        page_query = page_query.outerjoin(
            WebsiteHypeStatProfile,
            WebsiteHypeStatProfile.website_id == Website.id,
        )
    page_query = (
        page_query.where(*conditions)
        .order_by(*ordering)
        .limit(limit)
        .offset(offset)
    )
    websites = list(db.scalars(page_query).all())
    return websites, total_count, indexed_count, website_count


def get_website_registry_facets(db: Session) -> dict[str, list[str]]:
    rows = db.execute(
        select(
            Website.site_topic,
            Website.discovery_source,
            Website.discovery_provider,
            WebsiteHypeStatProfile.payload,
        )
        .outerjoin(
            WebsiteHypeStatProfile,
            WebsiteHypeStatProfile.website_id == Website.id,
        )
        .where(Website.crawl_id.is_not(None))
    )
    categories: set[str] = set()
    discovery_sources: set[str] = set()
    hypestat_countries: set[str] = set()
    backlink_countries: set[str] = set()
    audience_countries: set[str] = set()
    for site_topic, source, provider, payload in rows:
        categories.update(site_topic or ["unclassified"])
        discovery_sources.add(source)
        if provider:
            discovery_sources.add(provider)
        data = _profile_data(payload)
        if data is None:
            continue
        rank = data.get("rank")
        country = rank.get("country") if isinstance(rank, dict) else None
        if isinstance(country, dict) and isinstance(country.get("country"), str):
            hypestat_countries.add(country["country"])
        backlinks = data.get("backlinks")
        if isinstance(backlinks, dict):
            backlink_rows = backlinks.get("by_country")
            if not isinstance(backlink_rows, list):
                backlink_rows = []
            for item in backlink_rows:
                if isinstance(item, dict) and isinstance(item.get("country"), str):
                    backlink_countries.add(item["country"])
        traffic = data.get("traffic")
        if isinstance(traffic, dict):
            audience_rows = traffic.get("countries")
            if not isinstance(audience_rows, list):
                audience_rows = []
            for item in audience_rows:
                country_name = (
                    item.get("country") or item.get("name")
                    if isinstance(item, dict)
                    else item
                )
                if isinstance(country_name, str) and country_name:
                    audience_countries.add(country_name)

    return {
        "categories": sorted(categories),
        "discovery_sources": sorted(discovery_sources),
        "hypestat_countries": sorted(hypestat_countries),
        "backlink_countries": sorted(backlink_countries),
        "audience_countries": sorted(audience_countries),
    }


def _profile_data(payload):
    if not isinstance(payload, dict):
        return None
    entry = payload.get("entry") or payload
    data = entry.get("data") if isinstance(entry, dict) else None
    return data if isinstance(data, dict) else None
