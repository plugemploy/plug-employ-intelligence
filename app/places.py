"""Find local businesses with the official Google Places API (New): Text Search.

Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
"""

from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import GOOGLE_PLACES_API_KEY, MONTHLY_API_BUDGET_USD
from app.models import Account, ApiUsage, Lead, Organization

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
# Asking for phone, website, rating and reviews makes this the "Enterprise" price tier.
# We count every request at the full list price and ignore Google's free monthly allowance,
# so real spending will be the same or lower than what the app shows.
COST_PER_REQUEST_USD = 0.035
FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.addressComponents",
    "places.businessStatus", "places.nationalPhoneNumber", "places.websiteUri",
    "places.rating", "places.userRatingCount", "nextPageToken",
])
MIN_REVIEWS = 5

# Fox Den (37934) and Sequoyah Hills (37919) first: they matter most.
TEST_ZIPS = ["37934", "37919", "37922", "37932", "37923", "37909"]
INDUSTRIES = [
    "Dentist", "Orthodontist", "Med spa", "Dermatology", "Chiropractor", "Physical therapy",
    "Salon", "Fitness", "Restaurant", "Wine shop", "Boutique", "Jeweler", "Golf", "Event venue",
    "HVAC", "Roofing", "Remodeling", "Pool", "Interior design", "Pest control", "Landscaping", "Auto repair",
    "Real estate", "Financial advisor", "Insurance agent", "Law firm", "Mortgage lender",
    "Private school", "Tutoring", "Kids activities", "Veterinarian", "Pet grooming",
]


class BudgetExceeded(Exception):
    pass


def month_start() -> datetime:
    return datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent_this_month(db: Session, account_id: int | None = None) -> float:
    """Estimated API spending this calendar month: one account, or all accounts when account_id is None."""
    q = select(func.coalesce(func.sum(ApiUsage.est_cost_usd), 0)).where(ApiUsage.created_at >= month_start())
    if account_id is not None:
        q = q.where(ApiUsage.account_id == account_id)
    return float(db.scalar(q))


def check_budget(db: Session, account_id: int, next_cost: float) -> None:
    """Stop before going over the account's own monthly limit (the owner's is MONTHLY_API_BUDGET_USD)."""
    from app.plans import monthly_limit, trial_over
    account = db.get(Account, account_id)
    limit = monthly_limit(db, account) if account else 0
    if limit == 0 and account and trial_over(db.get(Organization, account.org_id) if account.org_id else None):
        raise BudgetExceeded("Your free trial has ended. Choose a plan on the Billing page to keep searching.")
    if spent_this_month(db, account_id) + next_cost > limit:
        raise BudgetExceeded("This month's search allowance is used up. Searches resume on the 1st.")


RESEARCH_AFTER_DAYS = 30  # a ZIP + industry search isn't repeated within this many days


def recently_searched(db: Session, account_id: int) -> set[str]:
    """Searches ("Dentist in 37934") this account ran in the last 30 days."""
    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(days=RESEARCH_AFTER_DAYS)
    return set(db.scalars(select(ApiUsage.detail).where(
        ApiUsage.account_id == account_id, ApiUsage.service == "google_places_text_search",
        ApiUsage.created_at >= since)))


def fetch_page(query: str, page_token: str | None = None) -> dict:
    """One request to Google. Kept separate so tests can replace it with fake data."""
    body = {"textQuery": query, "pageSize": 20}
    if page_token:
        body["pageToken"] = page_token
    headers = {"X-Goog-Api-Key": GOOGLE_PLACES_API_KEY, "X-Goog-FieldMask": FIELDS}
    resp = httpx.post(SEARCH_URL, json=body, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _zip_of(place: dict) -> str:
    for c in place.get("addressComponents", []):
        if "postal_code" in c.get("types", []):
            return c.get("shortText") or c.get("longText", "")
    return ""


def search_and_save(db: Session, account_id: int, zip_code: str, industry: str, max_pages: int = 1) -> dict:
    """Search one industry in one ZIP and save qualifying businesses as New leads."""
    stats = {"zip": zip_code, "industry": industry, "requests": 0, "found": 0, "added": 0,
             "skipped_closed": 0, "skipped_few_reviews": 0, "skipped_other_zip": 0, "skipped_duplicate": 0}
    existing = set(db.scalars(select(Lead.google_place_id).where(
        Lead.account_id == account_id, Lead.google_place_id.is_not(None))))
    token = None
    for _ in range(max_pages):
        check_budget(db, account_id, COST_PER_REQUEST_USD)
        data = fetch_page(f"{industry} in {zip_code}", token)
        db.add(ApiUsage(account_id=account_id, service="google_places_text_search",
                        detail=f"{industry} in {zip_code}", est_cost_usd=COST_PER_REQUEST_USD))
        db.commit()
        stats["requests"] += 1
        for p in data.get("places", []):
            stats["found"] += 1
            if p.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
                stats["skipped_closed"] += 1
            elif (p.get("userRatingCount") or 0) < MIN_REVIEWS:
                stats["skipped_few_reviews"] += 1
            elif _zip_of(p) != zip_code:
                stats["skipped_other_zip"] += 1
            elif p["id"] in existing:
                stats["skipped_duplicate"] += 1
            else:
                existing.add(p["id"])
                db.add(Lead(account_id=account_id, google_place_id=p["id"],
                            business_name=p.get("displayName", {}).get("text", "Unknown"),
                            address=p.get("formattedAddress", ""), zip_code=zip_code,
                            phone=p.get("nationalPhoneNumber", ""), website=p.get("websiteUri", ""),
                            industry=industry, status="New", rating=p.get("rating"),
                            review_count=p.get("userRatingCount")))
                stats["added"] += 1
        db.commit()
        token = data.get("nextPageToken")
        if not token:
            break
    return stats
