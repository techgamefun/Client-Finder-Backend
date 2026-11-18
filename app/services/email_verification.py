from email.headerregistry import Address

import dns.exception
import dns.resolver
from fastapi import HTTPException

from app.services.email_suppression import normalize_email


def verify_email_mx(value: str) -> tuple[str, str | None]:
    try:
        normalized = normalize_email(value)
        domain = Address(addr_spec=normalized).domain
    except HTTPException:
        return "invalid", None

    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=4)
        exchanges = [
            str(getattr(answer, "exchange", ""))
            for answer in answers
        ]
        if exchanges and all(exchange == "." for exchange in exchanges):
            return "no_mx", None
        if any(exchange and exchange != "." for exchange in exchanges):
            return "valid", None
    except dns.resolver.NoAnswer:
        pass
    except dns.resolver.NXDOMAIN:
        return "no_mx", None
    except dns.exception.DNSException:
        return "dns_error", None

    for record_type in ("A", "AAAA"):
        try:
            dns.resolver.resolve(domain, record_type, lifetime=4)
            return "valid", None
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except dns.exception.DNSException:
            return "dns_error", None
    return "no_mx", None
