
import pytest
from pydantic import ValidationError

from app.schemas.company import CompanyCreate


def test_valid_company_data():
    company = CompanyCreate(
        name="Example Manufacturing Pvt Ltd",
        website="https://example.com",
        domain="example.com",
        industry="Manufacturing",
        city="Kolkata",
        state="West Bengal",
    )

    assert company.name == "Example Manufacturing Pvt Ltd"
    assert company.domain == "example.com"
    assert str(company.website).startswith("https://example.com")


def test_company_name_is_required():
    with pytest.raises(ValidationError):
        CompanyCreate(
            name="",
        )


def test_invalid_website_is_rejected():
    with pytest.raises(ValidationError):
        CompanyCreate(
            name="Example Company",
            website="not-a-valid-url",
        )


def test_optional_fields_can_be_missing():
    company = CompanyCreate(
        name="Example Company",
    )

    assert company.name == "Example Company"
    assert company.website is None
    assert company.industry is None
