"""Shared test helpers: sign a test client in as the owner of a given account."""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import auth
from app.db import engine
from app.models import Account, Organization, User


def login_as(client, account_id: int, role: str = "owner") -> int:
    """Create (or reuse) a user for this account and put their session cookie on the client."""
    with Session(engine) as db:
        account = db.get(Account, account_id)
        if account.org_id is None:
            org = Organization(name=f"Test: org {account_id}")
            db.add(org)
            db.flush()
            account.org_id = org.id
        if account.setup_done_at is None:
            from datetime import datetime, timezone
            account.setup_done_at = datetime.now(timezone.utc)
        email = f"test-{account_id}-{role}@example.com"
        user = db.scalars(select(User).where(User.email == email)).first()
        if user is None:
            user = User(org_id=account.org_id, account_id=account_id, email=email, role=role)
            db.add(user)
        db.commit()
        client.cookies.set(auth.SESSION_COOKIE, auth.make_session(user.id))
        return user.id


def on_plan(account_id: int, tier: str) -> None:
    """Give an account its own test company on the given plan."""
    with Session(engine) as db:
        org = Organization(name=f"Test: {tier} org {account_id}", plan="active", plan_tier=tier)
        db.add(org)
        db.flush()
        db.get(Account, account_id).org_id = org.id
        db.commit()


@pytest.fixture(autouse=True, scope="session")
def _cleanup_test_orgs():
    yield
    with Session(engine) as db:
        db.execute(delete(Organization).where(Organization.name.like("Test:%")))
        db.commit()


@pytest.fixture(autouse=True)
def _no_overture_network(monkeypatch):
    """Tests never read Overture online: searches see no nearby places unless a test supplies them."""
    from app import overture

    def blocked(*args):
        raise RuntimeError("Tests don't read Overture online; replace fetch_places / fetch_nearby.")
    monkeypatch.setattr(overture, "_read_area", blocked)
    monkeypatch.setattr(overture, "fetch_nearby", lambda zip_code: [])
