"""Subscription plans and their monthly usage allowance.

The allowance is an estimate of API cost (Google searches, Claude scoring and drafts) included in the
price. Customers see it as "searches left"; searches pause when it runs out. New companies start on
Beta (free, everything included) for 30 days, then pick a paid plan on the Billing page.
"""

import math
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import MONTHLY_API_BUDGET_USD
from app.models import Account, Organization

# searches: ZIP x business-type searches a month (None = no count). allowance_usd: a cap on the Claude cost
# (scoring, drafts) each month, as a safety net. nightly_max: most searches per night.
# email: Gmail features (Emailed tags, quiet follow-ups, drafts, Monday digest). close: Close CRM connection.
# max_zips / max_industries: territory size ("unlimited" searches within it).
# Since 2026-10-28 one plan is sold: "pro", $49/month with everything. Its $6.50 Claude cap keeps a 77% margin
# at full use (640 searches x ~$0.006 = ~$3.84, the rest covers drafts, reasons to reach out and re-scores).
PLANS = {
    "beta": {"label": "Beta", "price": 0, "searches": 380, "allowance_usd": 15.0, "nightly_max": 20, "email": True, "close": True},
    "pro": {"label": "Plug Employ", "price": 49, "searches": 640, "allowance_usd": 6.5, "nightly_max": 30, "email": True, "close": True},
    # Sign-ups from the /try link (email campaign, 2026-10-28): one search ever, then the $49 pop-up.
    "free": {"label": "Free search", "price": 0, "searches": 1, "lifetime": True, "allowance_usd": 0.5, "nightly_max": 0,
             "email": False, "close": False, "max_zips": 1, "max_industries": 1},
    # Earlier plans, kept for companies already on them. Not offered on the Billing page.
    "personal": {"label": "Standard", "price": 99, "searches": 380, "allowance_usd": 15.0, "nightly_max": 20, "email": False, "close": False},
    "premium": {"label": "Premium", "price": 299, "searches": 1400, "allowance_usd": 55.0, "nightly_max": 50, "email": True, "close": True,
                "max_zips": 20, "max_industries": 70, "unlimited": True},
    "founding": {"label": "Founding member", "price": 79, "searches": 380, "allowance_usd": 15.0, "nightly_max": 20, "email": True, "close": True},
    "wholesale": {"label": "Wholesale (per publisher)", "price": 60, "searches": 256, "allowance_usd": 10.0, "nightly_max": 20, "email": True, "close": True},
    "owner": {"label": "Owner", "price": 0, "searches": None, "allowance_usd": None, "nightly_max": 100, "email": True, "close": True},  # only the .env cap applies
}
EMAIL_PLAN_MSG = "Gmail features are included in the $49 Plug Employ plan. Switch on the Billing page."
TRIAL_DAYS = 30  # a company that hasn't subscribed gets this long, then searches pause
# Searches come from Overture's free open data; what a search costs is Claude scoring the leads it adds.
SCORING_COST_PER_SEARCH = 0.006  # measured ~$0.004 for Google's 20 results; Overture adds up to 30
SEARCH_SERVICES = ("google_places_text_search", "overture_search")  # api_usage rows that count as a search
WARN_AT = 0.8


def _org(db: Session, account: Account) -> Organization | None:
    return db.get(Organization, account.org_id) if account and account.org_id else None


def plan_for(db: Session, account: Account) -> dict:
    org = _org(db, account)
    return PLANS.get(org.plan_tier if org else "personal", PLANS["personal"])


def trial_ends(org: Organization) -> datetime | None:
    """When an unpaid company's free time runs out (None once they subscribe, or for the owner)."""
    if org.plan != "trial" or org.plan_tier in ("owner", "free") or org.created_at is None:  # free: its one search is the limit
        return None
    return org.created_at + timedelta(days=TRIAL_DAYS)


def trial_over(org: Organization | None) -> bool:
    ends = trial_ends(org) if org else None
    return ends is not None and datetime.now(timezone.utc) >= ends


def has_close(db: Session, account: Account) -> bool:
    """Whether this account's plan includes the Close CRM connection."""
    return bool(account) and plan_for(db, account).get("close", False)


def has_email(db: Session, account: Account) -> bool:
    """Whether this account's plan includes the Gmail features."""
    return bool(account) and plan_for(db, account)["email"]


def territory_error(plan: dict, zips: list[str], industries: list[str] | None = None) -> str:
    """A message when a plan's ZIP or business-type limit is exceeded, else ""."""
    if plan.get("max_zips") and len(zips) > plan["max_zips"]:
        n = plan["max_zips"]
        return f"The {plan['label']} plan covers up to {n} ZIP code{'s' if n != 1 else ''}. You entered {len(zips)}."
    if industries is not None and plan.get("max_industries") and len(industries) > plan["max_industries"]:
        n = plan["max_industries"]
        return (f"The {plan['label']} plan covers up to {n} business type{'s' if n != 1 else ''}. "
                f"You picked {len(industries)}.")
    return ""


def monthly_limit(db: Session, account: Account) -> float:
    org = _org(db, account)
    if org and (org.plan == "cancelled" or trial_over(org)):
        return 0.0  # no active subscription: searches pause, everything else keeps working
    allowance = plan_for(db, account)["allowance_usd"]
    return MONTHLY_API_BUDGET_USD if allowance is None else allowance


def searches_this_month(db: Session, account_id: int, ever: bool = False) -> int:
    """Searches run this month (or ever, for the one-time Free search)."""
    from sqlalchemy import func, select
    from app.models import ApiUsage
    from app.places import month_start
    q = select(func.count()).select_from(ApiUsage).where(
        ApiUsage.account_id == account_id, ApiUsage.service.in_(SEARCH_SERVICES))
    return db.scalar(q if ever else q.where(ApiUsage.created_at >= month_start())) or 0


def first_leads_at(db: Session, account_id: int) -> datetime | None:
    """When this account's first search finished (leads found, read and scored): a starter or nightly batch
    that searched something, or a Find leads search."""
    from sqlalchemy import func, select
    from app.models import NightlyRun, SearchJob
    times = [db.scalar(select(func.min(NightlyRun.finished_at)).where(
                 NightlyRun.account_id == account_id, NightlyRun.searches > 0)),
             db.scalar(select(func.min(SearchJob.finished_at)).where(
                 SearchJob.account_id == account_id, SearchJob.status == "done"))]
    times = [t for t in times if t]
    return min(times) if times else None


def usage(db: Session, account: Account) -> dict:
    """What the account has used and has left this month: searches, and the Claude cost (owner only sees $)."""
    from app.places import spent_this_month
    plan, org = plan_for(db, account), _org(db, account)
    limit, spent = monthly_limit(db, account), spent_this_month(db, account.id)
    paused = bool(org and (org.plan == "cancelled" or trial_over(org)))
    total = None if plan["searches"] is None else (0 if paused else plan["searches"])
    used = searches_this_month(db, account.id, ever=plan.get("lifetime", False))
    left = None if total is None else max(0, total - used)
    return {"limit": limit, "spent": spent, "left": max(0.0, limit - spent),
            "pct": (used / total if total else 1.0) if total is not None else 0.0,
            "searches_total": total, "searches_used": used, "searches_left": left,
            "plan": plan, "trial_over": trial_over(org), "trial_ends": trial_ends(org) if org else None}


def nightly_quota(db: Session, account: Account, per_night: int, today: date | None = None) -> int:
    """Spread what's left evenly over the rest of the month, never more than the nightly setting."""
    today = today or date.today()
    next_month = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    days_left = max(1, (next_month - today).days)
    per_night = min(per_night, plan_for(db, account)["nightly_max"])
    left = usage(db, account)["searches_left"]
    if left is None:  # no monthly count (owner)
        return per_night
    return max(0, min(per_night, math.ceil(left / days_left)))
