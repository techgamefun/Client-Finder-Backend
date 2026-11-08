
import re
import unicodedata


def normalize_company_name(name: str) -> str:
    """Create a comparison key for matching company names."""
    normalized = unicodedata.normalize("NFKC", name)
    normalized = normalized.casefold()
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)

    return normalized.strip()


def normalize_domain(value: str | None) -> str | None:
    """Normalize a domain or website URL for comparison."""
    if not value:
        return None

    value = value.strip().lower()

    if not value:
        return None

    if "://" not in value:
        value = f"//{value}"

    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    hostname = parsed.hostname

    if not hostname:
        return None

    hostname = hostname.rstrip(".")

    if hostname.startswith("www."):
        hostname = hostname[4:]

    return hostname or None
