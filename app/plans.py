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

# allowance_usd: Google + Claude cost included each month. nightly_max: most searches per night.
# email: Gmail features (Emailed tags, quiet follow-ups, drafts, Monday digest).
# max_zips / max_industries: territory size. On Premium these make searches "unlimited": every ZIP +
# business type is searched once a month, and 20 x 70 = 1,400 searches (~$55) still keeps a 77% margin at $299.
PLANS = {
    "beta": {"label": "Beta", "price": 0, "allowance_usd": 15.0, "nightly_max": 20, "email": True},
    "personal": {"label": "Standard", "price": 99, "allowance_usd": 15.0, "nightly_max": 20, "email": False},
    "pro": {"label": "Pro", "price": 149, "allowance_usd": 25.0, "nightly_max": 30, "email": True},
    "premium": {"label": "Premium", "price": 299, "allowance_usd": 55.0, "nightly_max": 50, "email": True,
                "max_zips": 20, "max_industries": 70, "unlimited": True},
    # Earlier plans, kept for companies already on them. Not offered on the Billing page.
    "founding": {"label": "Founding member", "price": 79, "allowance_usd": 15.0, "nightly_max": 20, "email": True},
    "wholesale": {"label": "Wholesale (per publisher)", "price": 60, "allowance_usd": 10.0, "nightly_max": 20, "email": True},
    "owner": {"label": "Owner", "price": 0, "allowance_usd": None, "nightly_max": 100, "email": True},  # only the .env cap applies
}
EMAIL_PLAN_MSG = "Gmail features are included in the Pro plan. Upgrade on the Billing page."
TRIAL_DAYS = 30  # a company that hasn't subscribed gets this long, then searches pause
# One search as customers see it: Google ($0.035, places.COST_PER_REQUEST_USD) plus Claude scoring
# the new leads it finds (~$0.004, measured).
SEARCH_COST = 0.039
WARN_AT = 0.8


def _org(db: Session, account: Account) -> Organization | None:
    return db.get(Organization, account.org_id) if account and account.org_id else None


def plan_for(db: Session, account: Account) -> dict:
    org = _org(db, account)
    return PLANS.get(org.plan_tier if org else "personal", PLANS["personal"])


def trial_ends(org: Organization) -> datetime | None:
    """When an unpaid company's free time runs out (None once they subscribe, or for the owner)."""
    if org.plan != "trial" or org.plan_tier == "owner" or org.created_at is None:
        return None
    return org.created_at + timedelta(days=TRIAL_DAYS)


def trial_over(org: Organization | None) -> bool:
    ends = trial_ends(org) if org else None
    return ends is not None and datetime.now(timezone.utc) >= ends


def has_email(db: Session, account: Account) -> bool:
    """Whether this account's plan includes the Gmail features."""
    return bool(account) and plan_for(db, account)["email"]


def territory_error(plan: dict, zips: list[str], industries: list[str] | None = None) -> str:
    """A message when a plan's ZIP or business-type limit is exceeded, else ""."""
    if plan.get("max_zips") and len(zips) > plan["max_zips"]:
        return f"The {plan['label']} plan covers up to {plan['max_zips']} ZIP codes. You entered {len(zips)}."
    if industries is not None and plan.get("max_industries") and len(industries) > plan["max_industries"]:
        return (f"The {plan['label']} plan covers up to {plan['max_industries']} business types. "
                f"You picked {len(industries)}.")
    return ""


def monthly_limit(db: Session, account: Account) -> float:
    org = _org(db, account)
    if org and (org.plan == "cancelled" or trial_over(org)):
        return 0.0  # no active subscription: searches pause, everything else keeps working
    allowance = plan_for(db, account)["allowance_usd"]
    return MONTHLY_API_BUDGET_USD if allowance is None else allowance


def usage(db: Session, account: Account) -> dict:
    """What the account has used and has left this month, in dollars and in searches."""
    from app.places import spent_this_month
    limit, spent = monthly_limit(db, account), spent_this_month(db, account.id)
    left = max(0.0, limit - spent)
    return {"limit": limit, "spent": spent, "left": left, "pct": (spent / limit) if limit else 1.0,
            "searches_total": int(limit // SEARCH_COST), "searches_left": int(left // SEARCH_COST),
            "plan": plan_for(db, account), "trial_over": trial_over(_org(db, account)),
            "trial_ends": trial_ends(_org(db, account)) if _org(db, account) else None}


def nightly_quota(db: Session, account: Account, per_night: int, today: date | None = None) -> int:
    """Spread what's left evenly over the rest of the month, never more than the nightly setting."""
    today = today or date.today()
    next_month = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    days_left = max(1, (next_month - today).days)
    per_night = min(per_night, plan_for(db, account)["nightly_max"])
    return max(0, min(per_night, math.ceil(usage(db, account)["searches_left"] / days_left)))
