"""Sign in with Google, and who can see what.

Sessions are an encrypted cookie holding the user id (14 days). Publishers see only their own
account; owners/admins can switch between the accounts in their organization.
"""

import json
import time
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, Request
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import APP_BASE_URL, APP_SECRET_KEY, GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
from app.db import get_session
from app.models import Account, Organization, User

SESSION_COOKIE = "session"
ACCOUNT_COOKIE = "account_id"
SESSION_DAYS = 14
SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"]
REDIRECT_URI = f"{APP_BASE_URL}/auth/callback"
# Before anyone has signed in, the first person to sign in on this Mac claims this company.
FIRST_OWNER_ORG = "Plug Employ"

# Everyone ticks the agreement box once (sign-up page, or /agree for people who signed up before it).
# Bump TERMS_VERSION when the terms change and everyone is asked again.
TERMS_VERSION = "2026-10-28"
REQUIRE_TERMS = True  # tests turn this off
AGREE_FREE = ("/agree", "/logout", "/ping")

_pending: dict[str, dict] = {}  # sign-in in progress: state -> {"verifier", "signup_company"}


class LoginRequired(Exception):
    pass


class TermsRequired(Exception):
    pass


def agree(user: User) -> None:
    user.terms_agreed_at = datetime.now(timezone.utc)
    user.terms_version = TERMS_VERSION


def _fernet() -> Fernet:
    return Fernet(APP_SECRET_KEY.encode())


def make_session(user_id: int) -> str:
    return _fernet().encrypt(json.dumps({"uid": user_id}).encode()).decode()


def read_session(token: str) -> int | None:
    try:
        return json.loads(_fernet().decrypt(token.encode(), ttl=SESSION_DAYS * 86400))["uid"]
    except (InvalidToken, ValueError, KeyError, TypeError):
        return None


def current_user(request: Request, db: Session = Depends(get_session)) -> User:
    uid = read_session(request.cookies.get(SESSION_COOKIE, ""))
    user = db.get(User, uid) if uid else None
    if user is None:
        raise LoginRequired()
    request.state.user = user
    if REQUIRE_TERMS and user.terms_version != TERMS_VERSION and request.url.path not in AGREE_FREE:
        raise TermsRequired()
    return user


def visible_accounts(db: Session, user: User) -> list[Account]:
    if user.is_admin:
        return list(db.scalars(select(Account).where(Account.org_id == user.org_id).order_by(Account.name)))
    return [db.get(Account, user.account_id)] if user.account_id else []


def current_account(request: Request, db: Session = Depends(get_session),
                    user: User = Depends(current_user)) -> Account | None:
    """The account this person is looking at. Never one outside their organization."""
    if user.is_admin:
        raw = request.cookies.get(ACCOUNT_COOKIE, "")
        chosen = db.get(Account, int(raw)) if raw.isdigit() else None
        if chosen and chosen.org_id == user.org_id:
            return chosen
    if user.account_id:
        return db.get(Account, user.account_id)
    accounts = visible_accounts(db, user)
    return accounts[0] if accounts else None


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise LoginRequired()
    return user


# ---- Google sign-in -------------------------------------------------------------------------

def _flow(**kw) -> Flow:
    config = {"web": {"client_id": GOOGLE_OAUTH_CLIENT_ID, "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                      "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                      "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [REDIRECT_URI]}}
    return Flow.from_client_config(config, scopes=SCOPES, redirect_uri=REDIRECT_URI, **kw)


def start_login(signup_company: str = "") -> str:
    flow = _flow()
    url, state = flow.authorization_url(prompt="select_account")
    _pending[state] = {"verifier": flow.code_verifier, "signup_company": signup_company.strip()[:200],
                       "at": time.time()}
    return url


def google_identity(state: str, code: str) -> tuple[dict, str]:
    """Returns ({email, name}, signup_company). Kept separate so tests can replace it."""
    pending = _pending.pop(state, None)
    if not pending:
        raise ValueError("That sign-in expired. Please try again.")
    flow = _flow(code_verifier=pending["verifier"])
    flow.fetch_token(code=code)
    info = id_token.verify_oauth2_token(flow.credentials.id_token, GoogleRequest(), GOOGLE_OAUTH_CLIENT_ID)
    if not info.get("email_verified"):
        raise ValueError("Google says this email isn't verified.")
    return {"email": info["email"].lower(), "name": info.get("name", "")}, pending["signup_company"]


def sign_in(db: Session, email: str, name: str, signup_company: str = "", tier: str = "beta") -> User:
    """Find or create the user for a verified Google email. tier is only used for a new company
    ("free" from the /try link); existing users keep what they have."""
    user = db.scalars(select(User).where(User.email == email)).first()
    if user is None and signup_company:
        org = Organization(name=signup_company, plan="trial", plan_tier=tier, signup_offer="free" if tier == "free" else "")
        db.add(org)
        db.flush()
        account = new_account(db, org.id, name or signup_company)
        user = User(org_id=org.id, account_id=account.id, email=email, name=name, role="owner")
        db.add(user)
    elif user is None and not db.scalar(select(func.count()).select_from(User)):
        org = db.scalars(select(Organization).where(Organization.name == FIRST_OWNER_ORG)).first()
        if org is None:
            raise ValueError("No account for this email yet. Sign up, or ask your company admin to invite you.")
        first = db.scalars(select(Account).where(Account.org_id == org.id).order_by(Account.id)).first()
        user = User(org_id=org.id, account_id=first.id if first else None, email=email, name=name, role="owner")
        db.add(user)
    elif user is None:
        # Anyone Google lets in (a Google Cloud test user while in beta) gets their own beta company,
        # named after them; the setup wizard runs next.
        return sign_in(db, email, name, name or email.split("@")[0], tier)
    if name and not user.name:
        user.name = name
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return user


def new_account(db: Session, org_id: int, name: str, zips: str = "") -> Account:
    """A fresh publisher workspace. Industries and description are chosen in the setup wizard."""
    account = Account(org_id=org_id, name=name[:200], target_zips=zips, industries="", monthly_budget_usd=50)
    db.add(account)
    db.flush()
    return account
