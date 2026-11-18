from types import SimpleNamespace

import dns.resolver

from app.services.email_verification import verify_email_mx


def test_email_verification_accepts_explicit_mx_record(monkeypatch):
    monkeypatch.setattr(
        "app.services.email_verification.dns.resolver.resolve",
        lambda domain, record_type, lifetime: (
            [SimpleNamespace(exchange="mx.example.test.")]
            if record_type == "MX"
            else []
        ),
    )

    assert verify_email_mx("person@example.test") == ("valid", None)


def test_email_verification_rejects_explicit_null_mx(monkeypatch):
    monkeypatch.setattr(
        "app.services.email_verification.dns.resolver.resolve",
        lambda domain, record_type, lifetime: (
            [SimpleNamespace(exchange=".")]
            if record_type == "MX"
            else ["192.0.2.10"]
        ),
    )

    assert verify_email_mx("person@example.test") == ("no_mx", None)


def test_email_verification_uses_implicit_mx_address_fallback(monkeypatch):
    def resolve(domain, record_type, lifetime):
        if record_type == "MX":
            raise dns.resolver.NoAnswer
        if record_type == "A":
            return ["192.0.2.10"]
        raise dns.resolver.NoAnswer

    monkeypatch.setattr(
        "app.services.email_verification.dns.resolver.resolve",
        resolve,
    )

    assert verify_email_mx("person@example.test") == ("valid", None)


def test_email_verification_rejects_domain_without_mail_routing(monkeypatch):
    monkeypatch.setattr(
        "app.services.email_verification.dns.resolver.resolve",
        lambda domain, record_type, lifetime: (_ for _ in ()).throw(
            dns.resolver.NoAnswer
        ),
    )

    assert verify_email_mx("person@example.test") == ("no_mx", None)


def test_email_verification_rejects_malformed_address_without_dns(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("DNS should not run for an invalid address.")

    monkeypatch.setattr(
        "app.services.email_verification.dns.resolver.resolve",
        fail_if_called,
    )

    assert verify_email_mx("not-an-email") == ("invalid", None)
