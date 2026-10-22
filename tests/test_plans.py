"""Checks plan allowances, the searches-left meter, nightly pacing and skipped repeats (no cost)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import auth, places, plans
from app.db import engine
from app.main import app
from app.models import Account, ApiUsage, Organization, User


@pytest.fixture
def acct():
    from datetime import datetime, timezone
    with Session(engine) as db:
        org = Organization(name="Test: Plans Co", plan_tier="wholesale")
        db.add(org)
        db.flush()
        a = auth.new_account(db, org.id, "Test: Plans Pub", "37934")
        a.industries, a.setup_done_at = "Dentist,Salon", datetime.now(timezone.utc)
        u = User(org_id=org.id, account_id=a.id, email="p@plans.test", role="owner")
        db.add(u)
        db.commit()
        out = {"org": org.id, "acct": a.id, "user": u.id}
    yield out
    with Session(engine) as db:
        db.execute(delete(Organization).where(Organization.name.like("Test:%")))
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def spend(db, aid, usd, detail="x", service="claude_scoring"):
    db.add(ApiUsage(account_id=aid, service=service, detail=detail, est_cost_usd=usd))
    db.commit()


def test_allowance_by_plan(acct):
    with Session(engine) as db:
        a = db.get(Account, acct["acct"])
        u = plans.usage(db, a)
        assert (u["limit"], u["searches_total"], u["searches_left"]) == (10.0, 285, 285)
        spend(db, a.id, 8.5)
        assert plans.usage(db, a)["pct"] == pytest.approx(0.85)
        with pytest.raises(places.BudgetExceeded):
            places.check_budget(db, a.id, 1.6)
        db.get(Organization, acct["org"]).plan_tier = "personal"
        db.commit()
        places.check_budget(db, a.id, 1.6)  # $15 allowance on Personal


def test_nightly_quota_spreads_allowance(acct):
    with Session(engine) as db:
        a = db.get(Account, acct["acct"])
        assert plans.nightly_quota(db, a, 20, today=date(2026, 10, 2)) == 10   # 285 left / 30 days
        assert plans.nightly_quota(db, a, 5, today=date(2026, 10, 2)) == 5     # never above the setting
        spend(db, a.id, 10)
        assert plans.nightly_quota(db, a, 20, today=date(2026, 10, 2)) == 0    # used up: no searches


def test_meter_shows_and_repeats_skipped(acct, monkeypatch):
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.make_session(acct["user"]))
    assert "285</b> of 285 searches left" in c.get("/leads").text
    with Session(engine) as db:
        spend(db, acct["acct"], 0.035, "Dentist in 37934", "google_places_text_search")
    calls = []
    monkeypatch.setattr(places, "GOOGLE_PLACES_API_KEY", "x")
    import app.main as m
    monkeypatch.setattr(m, "GOOGLE_PLACES_API_KEY", "x")
    monkeypatch.setattr(places, "search_and_save", lambda db, aid, z, i: calls.append(i) or {
        "zip": z, "industry": i, "requests": 1, "found": 0, "added": 0, "skipped_closed": 0,
        "skipped_few_reviews": 0, "skipped_other_zip": 0, "skipped_duplicate": 0})
    r = c.post("/search", data={"zips": ["37934"], "industries": ["Dentist", "Salon"]})
    assert calls == ["Salon"] and "skipped (no charge)" in r.text
