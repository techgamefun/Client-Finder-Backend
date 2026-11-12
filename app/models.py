
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


company_search_keywords = Table(
    "company_search_keywords",
    Base.metadata,
    Column(
        "company_id",
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "search_keyword_id",
        Uuid(as_uuid=True),
        ForeignKey("search_keywords.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

pagespeed_scan_job_companies = Table(
    "pagespeed_scan_job_companies",
    Base.metadata,
    Column(
        "job_id",
        Uuid(as_uuid=True),
        ForeignKey("pagespeed_scan_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "company_id",
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str | None] = mapped_column(
        String(200),
        index=True,
    )
    website: Mapped[str | None] = mapped_column(String(500))
    domain: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
    )
    industry: Mapped[str | None] = mapped_column(String(150))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))

    software_need: Mapped[str | None] = mapped_column(Text)
    lead_score: Mapped[int | None] = mapped_column(Integer)
    lead_status: Mapped[str] = mapped_column(
        String(30),
        default="new",
        server_default="new",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )

    sources: Mapped[list["LeadSource"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )
    search_keywords: Mapped[list["SearchKeyword"]] = relationship(
        secondary=company_search_keywords,
        back_populates="companies",
        order_by="desc(SearchKeyword.searched_at)",
    )
    pagespeed_scans: Mapped[list["PageSpeedScan"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
        order_by="desc(PageSpeedScan.scanned_at)",
    )

    __table_args__ = (
        Index("ix_companies_name", "name"),
        Index("ix_companies_industry", "industry"),
    )


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    company_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(150))
    business_email: Mapped[str | None] = mapped_column(String(320))
    business_phone: Mapped[str | None] = mapped_column(String(50))

    is_publicly_listed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )

    company: Mapped["Company"] = relationship(
        back_populates="contacts",
    )


class LeadSource(Base):
    __tablename__ = "lead_sources"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    company_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )

    source_url: Mapped[str] = mapped_column(
        String(2000),
        nullable=False,
    )
    source_domain: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    source_type: Mapped[str | None] = mapped_column(String(100))
    evidence: Mapped[str | None] = mapped_column(Text)

    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    company: Mapped["Company"] = relationship(
        back_populates="sources",
    )

    __table_args__ = (
        UniqueConstraint("source_url", name="uq_lead_source_url"),
    )


class SearchKeyword(Base):
    __tablename__ = "search_keywords"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    keyword: Mapped[str] = mapped_column(String(300), nullable=False)
    searched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    companies: Mapped[list[Company]] = relationship(
        secondary=company_search_keywords,
        back_populates="search_keywords",
    )


class WebsiteCrawl(Base):
    __tablename__ = "website_crawls"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    website_url: Mapped[str] = mapped_column(
        String(2000),
        nullable=False,
        unique=True,
    )
    site_type: Mapped[str | None] = mapped_column(String(64))
    home_page_url: Mapped[str | None] = mapped_column(String(2000))
    home_content: Mapped[str | None] = mapped_column(Text)
    about_page_url: Mapped[str | None] = mapped_column(String(2000))
    about_content: Mapped[str | None] = mapped_column(Text)
    blog_page_url: Mapped[str | None] = mapped_column(String(2000))
    blog_content: Mapped[str | None] = mapped_column(Text)
    service_page_url: Mapped[str | None] = mapped_column(String(2000))
    service_content: Mapped[str | None] = mapped_column(Text)
    contact_page_url: Mapped[str | None] = mapped_column(String(2000))
    contact_content: Mapped[str | None] = mapped_column(Text)
    goals_page_url: Mapped[str | None] = mapped_column(String(2000))
    goals_content: Mapped[str | None] = mapped_column(Text)
    emails: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    social_links: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    crawled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class WebsiteCrawlJob(Base):
    __tablename__ = "website_crawl_jobs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    website_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="queued",
        server_default="queued",
        index=True,
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error: Mapped[str | None] = mapped_column(Text)
    crawl_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("website_crawls.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    crawl: Mapped[WebsiteCrawl | None] = relationship()


class ApplicationSettings(Base):
    __tablename__ = "application_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tavily_api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    crawl_max_pages: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=6,
        server_default="6",
    )
    crawl_max_depth: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=2,
        server_default="2",
    )


class PageSpeedScan(Base):
    __tablename__ = "pagespeed_scans"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    company_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    website_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    strategy: Mapped[str] = mapped_column(String(10), nullable=False)
    category_scores: Mapped[dict[str, int | None]] = mapped_column(
        JSON,
        nullable=False,
    )
    report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    company: Mapped["Company"] = relationship(
        back_populates="pagespeed_scans",
    )

    __table_args__ = (
        Index(
            "ix_pagespeed_scans_company_scanned_at",
            "company_id",
            "scanned_at",
        ),
    )


class PageSpeedScanJob(Base):
    __tablename__ = "pagespeed_scan_jobs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    company_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    website_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    strategy: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="queued",
        server_default="queued",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error: Mapped[str | None] = mapped_column(Text)
    scan_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("pagespeed_scans.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    companies: Mapped[list[Company]] = relationship(
        secondary=pagespeed_scan_job_companies,
    )
    scan: Mapped[PageSpeedScan | None] = relationship()

    __table_args__ = (
        Index(
            "ix_pagespeed_scan_jobs_status",
            "status",
            "created_at",
        ),
    )
