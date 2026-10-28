"""Stripe subscriptions (official Stripe library). Stripe hosts checkout and card details;
this app only stores Stripe's ids and the subscription status.

Flow: Billing page -> Stripe Checkout -> back to /billing/return (status saved) -> daily status sync.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import stripe

from app.config import APP_BASE_URL, STRIPE_SECRET_KEY
from app.models import Account, Organization
from app.plans import PLANS

SELLABLE = ("pro",)
PRODUCT_NAME = "Plug Employ Intelligence"
ACTIVE_STATUSES = ("trialing", "active", "past_due")  # past_due: Stripe is retrying the card
_price_ids: dict[str, str] = {}
log = logging.getLogger("billing")


def configured() -> bool:
    return bool(STRIPE_SECRET_KEY)


def client() -> stripe.StripeClient:
    """Kept separate so tests can replace it."""
    return stripe.StripeClient(STRIPE_SECRET_KEY)


def lookup_key(tier: str) -> str:
    return f"pei_{tier}_{PLANS[tier]['price']}_monthly"  # a new price gets a new Stripe price


def price_ids(sc) -> dict[str, str]:
    """Find (or create once) a monthly price in Stripe for each plan, by lookup key."""
    if len(_price_ids) == len(SELLABLE):
        return _price_ids
    found = sc.v1.prices.list(params={"lookup_keys": [lookup_key(t) for t in SELLABLE], "limit": 10})
    by_key = {lookup_key(t): t for t in SELLABLE}
    for p in found.data:
        _price_ids[by_key[p.lookup_key]] = p.id
    for tier in SELLABLE:
        if tier not in _price_ids:
            plan = PLANS[tier]
            product = sc.v1.products.create(params={"name": f"{PRODUCT_NAME}: {plan['label']}"})
            price = sc.v1.prices.create(params={
                "product": product.id, "currency": "usd", "unit_amount": plan["price"] * 100,
                "recurring": {"interval": "month"}, "lookup_key": lookup_key(tier)})
            _price_ids[tier] = price.id
    return _price_ids


def seats(db: Session, org: Organization) -> int:
    return max(1, db.scalar(select(func.count()).select_from(Account).where(Account.org_id == org.id)) or 1)


def checkout_url(db: Session, org: Organization, tier: str, email: str) -> str:
    if tier not in SELLABLE:
        raise ValueError("Unknown plan.")
    sc = client()
    quantity = seats(db, org) if tier == "wholesale" else 1
    meta = {"org_id": str(org.id), "tier": tier}
    params = {
        "mode": "subscription",
        "line_items": [{"price": price_ids(sc)[tier], "quantity": quantity}],
        "client_reference_id": str(org.id),
        "metadata": meta,
        "subscription_data": {"metadata": meta},
        "allow_promotion_codes": True,
        "success_url": f"{APP_BASE_URL}/billing/return?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{APP_BASE_URL}/billing",
    }
    if PLANS[tier].get("trial_days"):
        params["subscription_data"]["trial_period_days"] = PLANS[tier]["trial_days"]
    if org.stripe_customer_id:
        try:
            return sc.v1.checkout.sessions.create(params={**params, "customer": org.stripe_customer_id}).url
        except stripe.InvalidRequestError as exc:
            if exc.code != "resource_missing":
                raise
            org.stripe_customer_id = ""  # e.g. a test-mode customer after switching to the live key
            db.commit()
    return sc.v1.checkout.sessions.create(params={**params, "customer_email": email}).url


def apply_status(org: Organization, status: str) -> None:
    org.billing_status = status
    org.plan = "active" if status in ACTIVE_STATUSES else "cancelled"


def finish_checkout(db: Session, org: Organization, session_id: str) -> str:
    """Called when Stripe sends the customer back. Returns the plan label."""
    sc = client()
    session = sc.v1.checkout.sessions.retrieve(session_id)
    if session.client_reference_id != str(org.id):
        raise ValueError("That checkout belongs to a different account.")
    sub = sc.v1.subscriptions.retrieve(session.subscription)
    org.stripe_customer_id, org.stripe_subscription_id = session.customer, sub.id
    org.plan_tier = (session.metadata or {}).get("tier", org.plan_tier)
    apply_status(org, sub.status)
    db.commit()
    return PLANS[org.plan_tier]["label"]


def sync(db: Session, org: Organization) -> str:
    """Refresh one company's subscription status from Stripe (cancellations, failed payments)."""
    if not org.stripe_subscription_id:
        return org.billing_status
    try:
        sub = client().v1.subscriptions.retrieve(org.stripe_subscription_id)
    except stripe.InvalidRequestError as exc:
        if exc.code != "resource_missing":
            raise
        # Stripe has no such subscription, e.g. a test-mode one after switching to the live key:
        # forget it and the customer, and the company picks a plan again.
        log.warning("Org %s: subscription %s not found in Stripe; unlinked", org.id, org.stripe_subscription_id)
        org.stripe_subscription_id = org.stripe_customer_id = ""
        apply_status(org, "")
        db.commit()
        return ""
    apply_status(org, sub.status)
    db.commit()
    return sub.status


def sync_all(db: Session) -> None:
    for org in db.scalars(select(Organization).where(Organization.stripe_subscription_id != "")).all():
        try:
            sync(db, org)
        except Exception:  # one company's problem shouldn't stop the others' check
            log.exception("Stripe sync failed for org %s", org.id)
            db.rollback()


def portal_url(org: Organization) -> str:
    """Stripe's own page where customers change card, switch plan or cancel."""
    return client().v1.billing_portal.sessions.create(params={
        "customer": org.stripe_customer_id, "return_url": f"{APP_BASE_URL}/billing"}).url
