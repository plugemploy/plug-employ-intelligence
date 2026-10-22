"""Database tables. Every table with customer data has account_id."""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

STATUSES = ["New", "Contacted", "Interested", "Not interested", "Won"]


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Per-customer monthly API limit (subscription plans will set this).
    monthly_budget_usd: Mapped[float] = mapped_column(Numeric(10, 2), default=50, server_default="50")
    # "My business" page: what they sell and who they want, used to score leads.
    business_description: Mapped[str] = mapped_column(Text, default="", server_default="")
    target_zips: Mapped[str] = mapped_column(Text, default="", server_default="")  # comma-separated, first = most important
    industries: Mapped[str] = mapped_column(Text, default="", server_default="")  # comma-separated

    @property
    def zip_list(self) -> list[str]:
        return [z for z in self.target_zips.split(",") if z]

    @property
    def industry_list(self) -> list[str]:
        return [i for i in self.industries.split(",") if i]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    business_name: Mapped[str] = mapped_column(String(300))
    address: Mapped[str] = mapped_column(String(300), default="")
    zip_code: Mapped[str] = mapped_column(String(10), default="", index=True)
    phone: Mapped[str] = mapped_column(String(40), default="")
    website: Mapped[str] = mapped_column(String(300), default="")
    industry: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(30), default="New")
    google_place_id: Mapped[str | None] = mapped_column(String(200), index=True)
    rating: Mapped[float | None] = mapped_column(Float)
    review_count: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[int | None] = mapped_column(Integer, index=True)
    score_reason: Mapped[str] = mapped_column(Text, default="")
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Website check: "ok", "no_site", "blocked" (robots.txt says no) or "error".
    website_status: Mapped[str | None] = mapped_column(String(20))
    website_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    website_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the business matches someone in the owner's uploaded Gmail contacts.
    contacted_match: Mapped[str] = mapped_column(String(300), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    notes: Mapped[list["LeadNote"]] = relationship(
        back_populates="lead", order_by="LeadNote.created_at.desc()", cascade="all, delete-orphan"
    )
    follow_ups: Mapped[list["FollowUp"]] = relationship(
        back_populates="lead", order_by="FollowUp.due_date", cascade="all, delete-orphan"
    )

    @property
    def next_follow_up(self):
        open_ones = [f for f in self.follow_ups if not f.done]
        return open_ones[0] if open_ones else None

    @property
    def last_note(self):
        return self.notes[0] if self.notes else None


class LeadNote(Base):
    __tablename__ = "lead_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lead: Mapped[Lead] = relationship(back_populates="notes")


class FollowUp(Base):
    __tablename__ = "follow_ups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    task: Mapped[str] = mapped_column(String(300), default="")
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lead: Mapped[Lead] = relationship(back_populates="follow_ups")


class ApiUsage(Base):
    """One row per paid API request, so the monthly budget can be enforced."""

    __tablename__ = "api_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    service: Mapped[str] = mapped_column(String(50))
    detail: Mapped[str] = mapped_column(String(300), default="")
    est_cost_usd: Mapped[float] = mapped_column(Numeric(10, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class NightlySettings(Base):
    """One row per account: how the overnight lead search runs."""

    __tablename__ = "nightly_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    run_hour: Mapped[int] = mapped_column(Integer, default=2)
    searches_per_night: Mapped[int] = mapped_column(Integer, default=20)


class NightlyRun(Base):
    """Log of each overnight search, shown as the morning summary."""

    __tablename__ = "nightly_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    searches: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    high_scores: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class Contact(Base):
    """People from the owner's Google Contacts export, used to spot businesses already contacted."""

    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    organization: Mapped[str] = mapped_column(String(300), default="")
    email: Mapped[str] = mapped_column(String(300), default="")
    email_domain: Mapped[str] = mapped_column(String(200), default="", index=True)
