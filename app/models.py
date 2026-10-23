"""Database tables. Every table with customer data has account_id."""

import json
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

STATUSES = ["New", "Contacted", "Appointment set", "Appointment shown", "Sold", "Not interested"]
OUTCOMES = ("Sold", "Not interested")
# The sales steps in order. Each lead remembers when it first reached each one (see Lead.reach).
STAGES = {"Contacted": "contacted_at", "Appointment set": "appt_set_at", "Appointment shown": "appt_shown_at",
          "Sold": "sold_at"}


class Base(DeclarativeBase):
    pass


class Organization(Base):
    """A customer company (a publishing company, or one independent publisher). Owns accounts and users."""

    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    plan: Mapped[str] = mapped_column(String(30), default="trial", server_default="trial")  # trial, active, cancelled
    plan_tier: Mapped[str] = mapped_column(String(30), default="personal", server_default="personal")  # see app/plans.py
    # Stripe (billing). Card details live only at Stripe, never here.
    stripe_customer_id: Mapped[str] = mapped_column(String(100), default="", server_default="")
    stripe_subscription_id: Mapped[str] = mapped_column(String(100), default="", server_default="")
    billing_status: Mapped[str] = mapped_column(String(30), default="", server_default="")  # Stripe's status, e.g. trialing, active
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Account(Base):
    """One publisher's workspace: their leads, territory, connections. Belongs to an organization."""

    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # Per-customer monthly API limit (subscription plans will set this).
    monthly_budget_usd: Mapped[float] = mapped_column(Numeric(10, 2), default=50, server_default="50")
    # "My business" page: what they sell and who they want, used to score leads.
    business_description: Mapped[str] = mapped_column(Text, default="", server_default="")
    target_zips: Mapped[str] = mapped_column(Text, default="", server_default="")  # comma-separated, first = most important
    industries: Mapped[str] = mapped_column(Text, default="", server_default="")  # comma-separated
    # Connected Gmail (read-only). The access token is stored encrypted.
    gmail_address: Mapped[str] = mapped_column(String(300), default="", server_default="")
    gmail_token_enc: Mapped[str] = mapped_column(Text, default="", server_default="")
    gmail_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gmail_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # ticked "we don't sell your data"
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    digest_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    setup_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # finished the setup wizard
    close_auto_send: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")  # nightly 9+ leads
    # This account's own Close API key, stored encrypted (APP_SECRET_KEY), and the Close organization it opens.
    close_key_enc: Mapped[str] = mapped_column(Text, default="", server_default="")
    close_org_name: Mapped[str] = mapped_column(String(200), default="", server_default="")

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
    # Why it was Sold or Not interested; past results teach the scoring.
    outcome_reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contract_end: Mapped[date | None] = mapped_column(Date)  # for Sold leads: renewal reminder 30 days before
    # When the lead first reached each sales step (kept even if it later says no), for results.
    contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    appt_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    appt_shown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Sold leads: monthly recurring revenue, contract length, and the one-time Ad Management Fee.
    mrr: Mapped[float | None] = mapped_column(Numeric(10, 2))
    contract_months: Mapped[int | None] = mapped_column(Integer)
    ad_mgmt_fee: Mapped[float | None] = mapped_column(Numeric(10, 2))
    # Asked when marking Sold: would they have found this business without the app? "yes", "no" or "unsure".
    found_elsewhere: Mapped[str | None] = mapped_column(String(10))
    draft_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # Gmail draft made
    # Close CRM: where the VA calls. Status there syncs back here.
    close_lead_id: Mapped[str] = mapped_column(String(100), default="", server_default="", index=True)
    close_status: Mapped[str] = mapped_column(String(100), default="", server_default="")
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
    website_about_url: Mapped[str] = mapped_column(String(500), default="", server_default="")  # About/Team page read
    # Owner or manager, only when the business's own website names them.
    contact_name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    contact_title: Mapped[str] = mapped_column(String(100), default="", server_default="")
    founded_year: Mapped[int | None] = mapped_column(Integer)  # only when the business's own website states it
    signals_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # last look for news
    # From the Gmail check: latest email to/from the business's website domain.
    last_emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    emailed_domain: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # Deleted leads are hidden everywhere but the Deleted leads page, and can be restored.
    # Searches still skip them, so a deleted business doesn't come back.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
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

    def reach(self, status: str, when: datetime) -> None:
        """Record reaching a sales step, and the steps before it if they weren't recorded."""
        names = list(STAGES)
        if status in STAGES:
            for step in names[: names.index(status) + 1]:
                if getattr(self, STAGES[step]) is None:
                    setattr(self, STAGES[step], when)

    @property
    def contract_value(self) -> float:
        return float(self.mrr or 0) * (self.contract_months or 0)

    @property
    def next_follow_up(self):
        open_ones = [f for f in self.follow_ups if not f.done]
        return open_ones[0] if open_ones else None

    @property
    def last_note(self):
        return self.notes[0] if self.notes else None


SIGNAL_KINDS = {"opening": ("🎉", "Grand opening"), "new_location": ("📍", "New location"),
                "event": ("📅", "Event"), "anniversary": ("🎂", "Anniversary"), "hiring": ("🧑‍💼", "Hiring"),
                "new_service": ("✨", "New service"), "promotion": ("🏷️", "Promotion"), "award": ("🏆", "Award"),
                "expansion": ("🏗️", "Expansion")}


class LeadSignal(Base):
    """A timely reason to reach out, spotted on the business's own website (see app/signals.py)."""

    __tablename__ = "lead_signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # a SIGNAL_KINDS key
    text: Mapped[str] = mapped_column(String(300))
    event_date: Mapped[date | None] = mapped_column(Date)  # when the site gives one
    source_url: Mapped[str] = mapped_column(String(500), default="", server_default="")
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # handled or dismissed
    lead: Mapped["Lead"] = relationship()

    @property
    def icon(self) -> str:
        return SIGNAL_KINDS.get(self.kind, ("🔔", ""))[0]

    @property
    def label(self) -> str:
        return SIGNAL_KINDS.get(self.kind, ("", "News"))[1]


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



class SearchJob(Base):
    """One Find leads search, run in the background. The bar at the top of every page shows it."""

    __tablename__ = "search_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running", server_default="running")  # running, done, error
    step: Mapped[str] = mapped_column(String(50), default="", server_default="")  # what it's doing after searching
    zips: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")  # JSON lists
    industries: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    results: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")  # one dict per search done
    error: Mapped[str] = mapped_column(Text, default="", server_default="")
    seen: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")  # bar dismissed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def zip_list(self) -> list[str]:
        return json.loads(self.zips or "[]")

    @property
    def industry_list(self) -> list[str]:
        return json.loads(self.industries or "[]")

    @property
    def result_list(self) -> list[dict]:
        return json.loads(self.results or "[]")

    @property
    def total(self) -> int:
        return len(self.zip_list) * len(self.industry_list)

    @property
    def added(self) -> int:
        return sum(r.get("added", 0) for r in self.result_list)


WOULD_PAY = {"personal": "Yes, Standard ($99)", "pro": "Yes, Pro ($149)", "premium": "Yes, Premium ($299)",
             "unsure": "Not sure yet", "no": "No"}


class BetaSurvey(Base):
    """End-of-beta answers, asked once per account around day 25. The owner reads them on Beta results."""

    __tablename__ = "beta_surveys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    would_pay: Mapped[str] = mapped_column(String(20), default="", server_default="")  # a WOULD_PAY key
    recommend: Mapped[int | None] = mapped_column(Integer)  # 0-10
    hours_saved: Mapped[float | None] = mapped_column(Numeric(5, 1))  # per week
    quote: Mapped[str] = mapped_column(Text, default="", server_default="")
    quote_ok: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")  # may we quote it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Feedback(Base):
    """A note a tester sent from the Send feedback box. The owner reads them all on /feedback."""

    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    page: Mapped[str] = mapped_column(String(300), default="", server_default="")
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


ROLES = ("owner", "admin", "publisher")


class User(Base):
    """A person who signs in with Google. Owners/admins manage their organization; publishers use one account."""

    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), index=True)
    email: Mapped[str] = mapped_column(String(300), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    role: Mapped[str] = mapped_column(String(20), default="publisher", server_default="publisher")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_admin(self) -> bool:
        return self.role in ("owner", "admin")
