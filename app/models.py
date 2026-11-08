
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)

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

    __table_args__ = (
        Index("ix_companies_name", "name"),
        Index("ix_companies_industry", "industry"),
    )


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)

    company_id: Mapped[int] = mapped_column(
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

    id: Mapped[int] = mapped_column(primary_key=True)

    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )

    source_url: Mapped[str] = mapped_column(
        String(2000),
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
