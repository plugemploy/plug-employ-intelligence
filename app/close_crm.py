"""Close CRM connection (official Close API): send leads for the VA to call, and sync statuses back.

Calling stays in Close (local numbers, caller verification). This app never places calls.
API docs: https://developer.close.com
"""

import time
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import CLOSE_API_KEY
from app.gmail import site_domain
from app.models import OUTCOMES, FollowUp, Lead

BASE_URL = "https://api.close.com/api/v1/"
NEW_STATUS = "Potential"
TOP_N = 20

# Close status (lower case) -> (app status, outcome reason, follow-up in N days or "next_year")
STATUS_MAP = {
    "potential": ("New", "", None),
    "qualified": ("Contacted", "", None),
    "interested": ("Contacted", "", None),
    "1-3 months": ("Contacted", "", 30),
    "3-6 months": ("Contacted", "", 90),
    "next year": ("Contacted", "", "next_year"),
    "customer": ("Sold", "", None),
    "not interested": ("Not interested", "", None),
    "bad fit": ("Not interested", "Bad fit", None),
    "canceled": ("Not interested", "Cancelled (former customer)", None),
    "cancelled": ("Not interested", "Cancelled (former customer)", None),
    "do not call": ("Not interested", "Do not call", None),
}


# Each account connects its own Close (My business page): its API key is stored encrypted on the account.
# The key in .env is the owner's own Close; accounts in this organization fall back to it.
OWNER_ORG = "Plug Employ"


def key_for(db: Session, account) -> str:
    """The Close API key this account uses, or "" when it has none."""
    from app.gmail import _fernet
    from app.models import Organization
    if account is None:
        return ""
    if account.close_key_enc:
        return _fernet().decrypt(account.close_key_enc.encode()).decode()
    org = db.get(Organization, account.org_id) if account.org_id else None
    return CLOSE_API_KEY if (CLOSE_API_KEY and org and org.name == OWNER_ORG) else ""


def enabled_for(db: Session, account) -> bool:
    from app.plans import has_close
    return bool(account and has_close(db, account) and key_for(db, account))


def client_for(db: Session, account) -> "CloseClient":
    return CloseClient(key_for(db, account))


def connect(db: Session, account, api_key: str, client: "CloseClient | None" = None) -> str:
    """Check the key with Close, then save it encrypted. Returns the Close organization's name."""
    from app.gmail import _fernet
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("Paste your Close API key first.")
    client = client or CloseClient(api_key)
    try:
        orgs = client.me().get("organizations", [])
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise ValueError("Close didn't accept that key. Copy it again from Close: Settings > Developer > API Keys.")
        raise
    account.close_key_enc = _fernet().encrypt(api_key.encode()).decode()
    account.close_org_name = (orgs[0].get("name", "") if orgs else "")[:200]
    db.commit()
    return account.close_org_name


def disconnect(db: Session, account) -> None:
    """Forget this account's key. Leads keep their Close links in case it reconnects."""
    account.close_key_enc, account.close_org_name, account.close_auto_send = "", "", False
    db.commit()


class CloseClient:
    """Thin wrapper over Close's REST API. Kept separate so tests can replace it."""

    def __init__(self, api_key: str):
        self.http = httpx.Client(base_url=BASE_URL, auth=(api_key, ""), timeout=30)

    def _call(self, method: str, path: str, **kw) -> dict:
        for _ in range(5):
            r = self.http.request(method, path, **kw)
            if r.status_code == 429:  # rate limited: wait as Close asks
                time.sleep(float(r.headers.get("retry-after", "2")))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def me(self) -> dict:
        """The signed-in Close user, with the organizations the key opens."""
        return self._call("GET", "me/")

    def statuses(self) -> dict[str, str]:
        """label (lower case) -> status id"""
        return {s["label"].lower(): s["id"] for s in self._call("GET", "status/lead/").get("data", [])}

    def all_leads(self) -> list[dict]:
        """Every Close lead with id, name, url and status (paged)."""
        out, skip = [], 0
        while True:
            page = self._call("GET", "lead/", params={"_fields": "id,display_name,url,status_label",
                                                       "_limit": 200, "_skip": skip})
            out += page.get("data", [])
            if not page.get("has_more") or skip > 50_000:
                return out
            skip += 200

    def create_lead(self, body: dict) -> str:
        return self._call("POST", "lead/", json=body)["id"]

    def add_note(self, close_lead_id: str, note: str) -> None:
        self._call("POST", "activity/note/", json={"lead_id": close_lead_id, "note": note})


def _norm(name: str) -> str:
    return " ".join("".join(ch for ch in name.lower() if ch.isalnum() or ch == " ").split())


def lead_body(lead: Lead, status_id: str) -> dict:
    body = {"name": lead.business_name, "status_id": status_id,
            "description": f"Plug Employ score {lead.score}/10: {lead.score_reason}" if lead.score else ""}
    if lead.website:
        body["url"] = lead.website
    if lead.address or lead.zip_code:
        body["addresses"] = [{"label": "business", "address_1": lead.address.split(",")[0] if lead.address else "",
                              "zipcode": lead.zip_code, "country": "US"}]
    if lead.phone or lead.contact_name:
        contact = {"name": lead.contact_name or lead.business_name}
        if lead.contact_title:
            contact["title"] = lead.contact_title
        if lead.phone:
            contact["phones"] = [{"phone": lead.phone, "type": "office"}]
        body["contacts"] = [contact]
    return body


def note_text(lead: Lead) -> str:
    lines = [f"From Plug Employ Intelligence. Score {lead.score}/10." if lead.score else "From Plug Employ Intelligence."]
    if lead.score_reason:
        lines.append(f"Why: {lead.score_reason}")
    if lead.contact_name:
        lines.append(f"Ask for: {lead.contact_name}" + (f", {lead.contact_title}" if lead.contact_title else "")
                     + (f" (named on {lead.website_about_url or lead.website})" if lead.website else ""))
    if lead.rating:
        lines.append(f"Google: {lead.rating} stars, {lead.review_count} reviews. Industry: {lead.industry}.")
    if lead.last_emailed_at:
        lines.append(f"Last emailed (Gmail): {lead.last_emailed_at.astimezone():%b %-d, %Y}.")
    lines += [f"Note {n.created_at.astimezone():%b %-d}: {n.text}" for n in reversed(lead.notes)]
    return "\n".join(lines)


def send_leads(db: Session, leads: list[Lead], client: CloseClient) -> dict:
    """Create these leads in Close as Potential, skipping ones already there."""
    stats = {"sent": 0, "already_in_close": 0, "failed": 0}
    if not leads:
        return stats
    statuses = client.statuses()
    # Close starts every org with "Potential"; an org that renamed it gets its first status instead.
    status_id = statuses.get(NEW_STATUS.lower()) or next(iter(statuses.values()))
    existing = client.all_leads()
    by_domain = {site_domain(c["url"]): c for c in existing if c.get("url")}
    by_name = {_norm(c.get("display_name", "")): c for c in existing}
    for lead in leads:
        match = (by_domain.get(site_domain(lead.website)) if lead.website else None) or by_name.get(_norm(lead.business_name))
        if match:
            lead.close_lead_id, lead.close_status = match["id"], match.get("status_label", "")
            stats["already_in_close"] += 1
            continue
        try:
            close_id = client.create_lead(lead_body(lead, status_id))
            client.add_note(close_id, note_text(lead))
        except httpx.HTTPError:
            stats["failed"] += 1
            continue
        lead.close_lead_id, lead.close_status = close_id, NEW_STATUS
        stats["sent"] += 1
    db.commit()
    return stats


def top_unsent(db: Session, account_id: int, min_score: int = 0, limit: int = TOP_N) -> list[Lead]:
    return db.scalars(select(Lead).where(
        Lead.account_id == account_id, Lead.deleted_at.is_(None), Lead.close_lead_id == "", Lead.status.not_in(OUTCOMES),
        Lead.phone != "", Lead.score >= max(min_score, 1))
        .order_by(Lead.score.desc(), Lead.review_count.desc().nulls_last()).limit(limit)).all()


def _follow_up_due(days) -> date:
    if days == "next_year":
        return date(date.today().year + 1, 1, 2)
    return date.today() + timedelta(days=days)


def apply_status(db: Session, lead: Lead, close_label: str) -> bool:
    """Update the app lead from its Close status. Returns True if something changed."""
    if close_label == lead.close_status:
        return False
    lead.close_status = close_label
    mapped = STATUS_MAP.get(close_label.lower())
    if not mapped:
        return True  # e.g. FACEBOOK GEN: remember it, change nothing else
    status, reason, follow_days = mapped
    if lead.status != status and status in OUTCOMES:
        lead.outcome_at = datetime.now(timezone.utc)
    lead.status = status
    lead.reach(status, datetime.now(timezone.utc))
    if status in OUTCOMES:
        lead.outcome_reason = reason or lead.outcome_reason
    if follow_days is not None:
        db.add(FollowUp(account_id=lead.account_id, lead_id=lead.id, due_date=_follow_up_due(follow_days),
                        task=f"Close: {close_label}, check back"))
    return True


def sync_back(db: Session, account_id: int, client: CloseClient) -> dict:
    """Read the VA's status changes in Close and update the matching leads here."""
    stats = {"updated": 0}
    leads = db.scalars(select(Lead).where(Lead.account_id == account_id, Lead.deleted_at.is_(None), Lead.close_lead_id != "")).all()
    if not leads:
        return stats
    labels = {c["id"]: c.get("status_label", "") for c in client.all_leads()}
    for lead in leads:
        if lead.close_lead_id in labels and apply_status(db, lead, labels[lead.close_lead_id]):
            stats["updated"] += 1
    db.commit()
    return stats
