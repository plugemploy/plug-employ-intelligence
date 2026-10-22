"""Gmail check (official Gmail API, read-only): has this account emailed a lead's business?

For each lead with a website, Gmail is searched for mail to or from that website's domain.
Only the date of the latest match is used; message text is never read or stored.
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import APP_BASE_URL, APP_SECRET_KEY, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
from app.models import Account, FollowUp, Lead

READ = "https://www.googleapis.com/auth/gmail.readonly"
COMPOSE = "https://www.googleapis.com/auth/gmail.compose"  # create drafts, send the digest to yourself
SCOPES = [READ, COMPOSE]
REDIRECT_URI = f"{APP_BASE_URL}/gmail/callback"
FOLLOW_UP_AFTER_DAYS = 14
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "msn.com",
             "aol.com", "icloud.com", "me.com", "comcast.net", "att.net", "bellsouth.net", "charter.net"}
CLOSED = ("Won", "Not interested")

if REDIRECT_URI.startswith(("http://127.0.0.1", "http://localhost")):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # plain http is fine on this Mac only
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

_pending: dict[str, tuple[int, str]] = {}  # sign-in in progress: state -> (account_id, code_verifier)


def configured() -> bool:
    return bool(GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and APP_SECRET_KEY)


def _fernet() -> Fernet:
    return Fernet(APP_SECRET_KEY.encode())


def _flow(**kw) -> Flow:
    config = {"web": {"client_id": GOOGLE_OAUTH_CLIENT_ID, "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                      "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                      "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [REDIRECT_URI]}}
    return Flow.from_client_config(config, scopes=SCOPES, redirect_uri=REDIRECT_URI, **kw)


def start_connect(account_id: int) -> str:
    """Google's sign-in page URL for this account."""
    flow = _flow()
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    _pending[state] = (account_id, flow.code_verifier)
    return url


def finish_connect(db: Session, state: str, code: str) -> Account:
    if state not in _pending:
        raise ValueError("That Gmail sign-in expired. Please click Connect Gmail again.")
    account_id, verifier = _pending.pop(state)
    flow = _flow(code_verifier=verifier)
    flow.fetch_token(code=code)
    creds = flow.credentials
    profile = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()
    account = db.get(Account, account_id)
    account.gmail_address = profile.get("emailAddress", "")
    account.gmail_token_enc = _fernet().encrypt(creds.to_json().encode()).decode()
    db.commit()
    return account


def can_compose(account: Account) -> bool:
    """True if this Gmail connection allows drafts (connected after the compose permission was added)."""
    if not (account.gmail_token_enc and APP_SECRET_KEY):
        return False
    try:
        info = json.loads(_fernet().decrypt(account.gmail_token_enc.encode()))
    except Exception:
        return False
    return COMPOSE in (info.get("scopes") or [])


def disconnect(db: Session, account: Account) -> None:
    account.gmail_address, account.gmail_token_enc, account.gmail_checked_at = "", "", None
    db.commit()


def gmail_service(db: Session, account: Account):
    """Gmail API client for this account. Kept separate so tests can replace it."""
    info = json.loads(_fernet().decrypt(account.gmail_token_enc.encode()))
    creds = Credentials.from_authorized_user_info(info)  # keep the permissions that were actually granted
    if not creds.valid and creds.refresh_token:
        creds.refresh(GoogleRequest())
        account.gmail_token_enc = _fernet().encrypt(creds.to_json().encode()).decode()
        db.commit()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def latest_email_time(service, domain: str) -> datetime | None:
    """Time of the newest email to or from this domain, or None."""
    res = service.users().messages().list(userId="me", q=f"from:{domain} OR to:{domain}", maxResults=1).execute()
    msgs = res.get("messages") or []
    if not msgs:
        return None
    msg = service.users().messages().get(userId="me", id=msgs[0]["id"], format="minimal").execute()
    return datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)


def last_address(service, domain: str) -> str:
    """The email address at this domain from your most recent email with them, or ""."""
    res = service.users().messages().list(userId="me", q=f"from:{domain} OR to:{domain}", maxResults=1).execute()
    msgs = res.get("messages") or []
    if not msgs:
        return ""
    msg = service.users().messages().get(userId="me", id=msgs[0]["id"], format="metadata",
                                         metadataHeaders=["From", "To", "Cc"]).execute()
    for h in msg.get("payload", {}).get("headers", []):
        for addr in re.findall(r"[\w.+-]+@[\w.-]+", h.get("value", "")):
            if addr.lower().endswith("@" + domain):
                return addr.lower()
    return ""


def site_domain(url: str) -> str:
    host = urlsplit(url if "//" in url else f"//{url}").hostname or ""
    return host.removeprefix("www.")


def check_account(db: Session, account_id: int) -> dict:
    """Tag leads this account has emailed and add follow-ups for ones gone quiet."""
    stats = {"checked": 0, "emailed": 0, "follow_ups": 0}
    account = db.get(Account, account_id)
    if not (account and account.gmail_token_enc and configured()):
        return stats
    service = gmail_service(db, account)
    seen: dict[str, datetime | None] = {}
    now = datetime.now(timezone.utc)
    leads = db.scalars(select(Lead).where(Lead.account_id == account_id, Lead.deleted_at.is_(None), Lead.website != "")).all()
    for lead in leads:
        domain = site_domain(lead.website)
        if not domain or domain in FREE_MAIL:
            continue
        if domain not in seen:
            seen[domain] = latest_email_time(service, domain)
            stats["checked"] += 1
        last = seen[domain]
        lead.last_emailed_at, lead.emailed_domain = last, (domain if last else "")
        if not last:
            continue
        stats["emailed"] += 1
        quiet = now - last >= timedelta(days=FOLLOW_UP_AFTER_DAYS)
        has_open = any(not f.done for f in lead.follow_ups)
        if quiet and lead.status not in CLOSED and not has_open:
            db.add(FollowUp(account_id=account_id, lead_id=lead.id, due_date=date.today(),
                            task=f"Follow up: last emailed {last.astimezone():%b %-d, %Y}"))
            stats["follow_ups"] += 1
    account.gmail_checked_at = now
    db.commit()
    return stats
