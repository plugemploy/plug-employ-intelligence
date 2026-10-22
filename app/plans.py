"""Subscription plans and their monthly usage allowance.

The allowance is an estimate of API cost (Google searches, Claude scoring and drafts) included in the
price. Customers see it as "searches left"; searches pause when it runs out.
"""

import math
from datetime import date

from sqlalchemy.orm import Session

from app.config import MONTHLY_API_BUDGET_USD
from app.models import Account, Organization

PLANS = {
    "beta": {"label": "Beta", "price": 49, "allowance_usd": 8.0, "trial_days": 30},
    "founding": {"label": "Founding member", "price": 79, "allowance_usd": 15.0},
    "personal": {"label": "Personal", "price": 99, "allowance_usd": 15.0},
    "wholesale": {"label": "Wholesale (per publisher)", "price": 60, "allowance_usd": 10.0},
    "owner": {"label": "Owner", "price": 0, "allowance_usd": None},  # only the overall .env cap applies
}
SEARCH_COST = 0.035  # keep in step with places.COST_PER_REQUEST_USD
WARN_AT = 0.8


def plan_for(db: Session, account: Account) -> dict:
    org = db.get(Organization, account.org_id) if account.org_id else None
    return PLANS.get(org.plan_tier if org else "personal", PLANS["personal"])


def monthly_limit(db: Session, account: Account) -> float:
    org = db.get(Organization, account.org_id) if account.org_id else None
    if org and org.plan == "cancelled":
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
            "plan": plan_for(db, account)}


def nightly_quota(db: Session, account: Account, per_night: int, today: date | None = None) -> int:
    """Spread what's left evenly over the rest of the month, never more than the nightly setting."""
    today = today or date.today()
    next_month = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    days_left = max(1, (next_month - today).days)
    return max(0, min(per_night, math.ceil(usage(db, account)["searches_left"] / days_left)))
