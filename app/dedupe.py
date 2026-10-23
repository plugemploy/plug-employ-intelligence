"""Spotting a business that's already in an account's leads (deleted ones too)."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.gmail import site_domain
from app.models import Lead

NAME_FILLER = {"the", "llc", "inc", "co", "company", "corp", "ltd", "and"}
# Sites many businesses share, so a matching domain doesn't mean the same business.
SHARED_DOMAINS = {"facebook.com", "instagram.com", "google.com", "business.site", "yelp.com", "linktr.ee",
                  "sites.google.com", "squareup.com", "square.site", "wixsite.com"}


def _name_key(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("&", " and ")).split()
    return " ".join(w for w in words if w not in NAME_FILLER)


def _phone_key(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 7 else ""


def find_duplicate(db: Session, account_id: int, business_name: str, zip_code: str, phone: str, website: str) -> Lead | None:
    """An existing lead (deleted ones too) that looks like the same business: same phone, same website,
    or same name in the same ZIP."""
    name, zip_code, phone = _name_key(business_name), zip_code.strip(), _phone_key(phone)
    domain = site_domain(website.strip().lower()) if website.strip() else ""
    if domain in SHARED_DOMAINS:
        domain = ""
    for lead in db.scalars(select(Lead).where(Lead.account_id == account_id).order_by(Lead.deleted_at.nulls_first(), Lead.id)):
        if phone and _phone_key(lead.phone) == phone:
            return lead
        if domain and lead.website and site_domain(lead.website.lower()) == domain:
            return lead
        if name and _name_key(lead.business_name) == name and (not zip_code or not lead.zip_code or lead.zip_code == zip_code):
            return lead
    return None
