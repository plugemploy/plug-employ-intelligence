"""Spot leads the owner has already emailed, using their Google Contacts export (CSV).

Matching: the lead's website domain equals a contact's email domain (free mail like gmail.com
is ignored), or the business name matches a contact's organization.
"""

import csv
import io
import re
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Contact, Lead

FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "msn.com",
             "aol.com", "icloud.com", "me.com", "mac.com", "comcast.net", "att.net", "bellsouth.net",
             "charter.net", "protonmail.com", "proton.me", "ymail.com"}
NAME_NOISE = {"the", "llc", "inc", "co", "pllc", "pc", "dds", "md", "and", "of", "company", "group"}


def norm_name(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    return " ".join(w for w in words if w not in NAME_NOISE)


def site_domain(url: str) -> str:
    host = urlsplit(url if "//" in url else f"//{url}").hostname or ""
    return host.removeprefix("www.")


def parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    email_cols = [h for h in headers if ("mail" in h.lower() and "value" in h.lower())
                  or h.lower() in ("email", "e-mail", "email address", "e-mail address")]
    if not email_cols:
        raise ValueError("That file doesn't look like a Google Contacts export (no email column found).")
    org_cols = [h for h in headers if h.lower() in ("organization name", "organization 1 - name", "company")]
    out = []
    for row in reader:
        name = (row.get("Name") or f"{row.get('First Name', '')} {row.get('Last Name', '')}").strip()
        org = next((row[c].strip() for c in org_cols if (row.get(c) or "").strip()), "")
        for col in email_cols:
            for email in (row.get(col) or "").split(":::"):
                email = email.strip().lower()
                if "@" in email:
                    out.append({"name": name[:300], "organization": org[:300], "email": email[:300],
                                "email_domain": email.rsplit("@", 1)[1][:200]})
    return out


def add_contacts(db: Session, account_id: int, text: str) -> int:
    """Add contacts from a CSV export, skipping emails already loaded. Returns how many were new."""
    have = set(db.scalars(select(Contact.email).where(Contact.account_id == account_id)))
    added = 0
    for c in parse_csv(text):
        if c["email"] not in have:
            have.add(c["email"])
            db.add(Contact(account_id=account_id, **c))
            added += 1
    db.commit()
    return added


def match_leads(db: Session, account_id: int) -> int:
    """Mark leads that match a contact. Returns how many leads are marked."""
    contacts = db.scalars(select(Contact).where(Contact.account_id == account_id)).all()
    by_domain = {c.email_domain: c.email for c in contacts if c.email_domain not in FREE_MAIL}
    orgs = {norm_name(c.organization): c.organization for c in contacts if len(norm_name(c.organization)) >= 5}
    marked = 0
    for lead in db.scalars(select(Lead).where(Lead.account_id == account_id)):
        match = by_domain.get(site_domain(lead.website or "")) if lead.website else None
        if not match:
            name = norm_name(lead.business_name)
            match = next((orig for o, orig in orgs.items() if o == name or (o in name and len(o) >= 8)), None)
        lead.contacted_match = (match or "")[:300]
        marked += bool(match)
    db.commit()
    return marked
