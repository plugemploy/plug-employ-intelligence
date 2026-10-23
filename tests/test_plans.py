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
        assert (u["limit"], u["searches_total"], u["searches_left"]) == (10.0, 256, 256)  # $10 / $0.039 per search
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
        assert plans.nightly_quota(db, a, 20, today=date(2026, 10, 2)) == 9    # 256 left / 30 days
        assert plans.nightly_quota(db, a, 5, today=date(2026, 10, 2)) == 5     # never above the setting
        spend(db, a.id, 10)
        assert plans.nightly_quota(db, a, 20, today=date(2026, 10, 2)) == 0    # used up: no searches


def test_meter_shows_and_repeats_skipped(acct, monkeypatch):
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.make_session(acct["user"]))
    assert "256</b> of 256 searches left" in c.get("/leads").text
    with Session(engine) as db:
        spend(db, acct["acct"], 0.035, "Dentist in 37934", "google_places_text_search")
    calls = []
    monkeypatch.setattr(places, "GOOGLE_PLACES_API_KEY", "x")
    import app.main as m
    monkeypatch.setattr(m, "GOOGLE_PLACES_API_KEY", "x")
    monkeypatch.setattr(places, "search_and_save", lambda db, aid, z, i: calls.append(i) or {
        "zip": z, "industry": i, "requests": 1, "found": 0, "added": 0, "skipped_closed": 0,
        "skipped_few_reviews": 0, "skipped_other_zip": 0, "skipped_duplicate": 0})
    from app import search_jobs
    monkeypatch.setattr(search_jobs, "spawn", lambda fn, *a: fn(*a))  # run the search right away
    r = c.post("/search", data={"zips": ["37934"], "industries": ["Dentist", "Salon"]}, follow_redirects=False)
    assert r.headers["location"] == "/leads?sort=score"
    assert calls == ["Salon"] and "skipped (no charge)" in c.get("/search").text


def test_search_runs_in_background_with_bar(acct, monkeypatch):
    from app import search_jobs
    from app.models import SearchJob
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.make_session(acct["user"]))
    import app.main as m
    monkeypatch.setattr(m, "GOOGLE_PLACES_API_KEY", "x")
    started = []
    monkeypatch.setattr(search_jobs, "spawn", lambda fn, *a: started.append(a))  # leave it "running"
    c.post("/search", data={"zips": ["37934"], "industries": ["Dentist", "Salon"]})
    assert len(started) == 1
    page = c.get("/leads").text
    assert "Generating new leads" in page and "Searching 0 of 2" in page
    r = c.post("/search", data={"zips": ["37934"], "industries": ["Dentist"]})
    assert "already running" in r.text and len(started) == 1  # no second search
    monkeypatch.setattr(places, "search_and_save", lambda db, aid, z, i: {
        "zip": z, "industry": i, "requests": 1, "found": 5, "added": 3, "skipped_closed": 0,
        "skipped_few_reviews": 2, "skipped_other_zip": 0, "skipped_duplicate": 0})
    search_jobs.run(started[0][0])
    bar = c.get("/search/status").text
    assert "6 new leads added" in bar and "every 3s" not in bar  # done: stops refreshing
    assert "6 new leads added" in c.get("/due").text  # the bar shows on every page
    c.post("/search/dismiss")
    assert "new leads added" not in c.get("/leads").text
    with Session(engine) as db:
        assert db.get(SearchJob, started[0][0]).status == "done"


def test_premium_territory_limits_and_unlimited_meter(acct):
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.make_session(acct["user"]))
    with Session(engine) as db:
        db.get(Organization, acct["org"]).plan_tier = "premium"
        db.commit()
    assert "Unlimited searches" in c.get("/leads").text
    zips = ", ".join(str(37000 + i) for i in range(21))
    r = c.post("/business", data={"description": "x", "zips": zips, "industries": ["Dentist"]})
    assert "up to 20 ZIP codes" in r.text
    r = c.post("/business", data={"description": "x", "zips": "37934", "extra_industries": ", ".join(f"Type {i}" for i in range(71))})
    assert "up to 70 business types" in r.text
    with Session(engine) as db:
        assert db.get(Account, acct["acct"]).target_zips == "37934"  # nothing saved
    c.post("/business", data={"description": "x", "zips": ", ".join(str(37000 + i) for i in range(20)), "industries": ["Dentist"]})
    with Session(engine) as db:
        assert len(db.get(Account, acct["acct"]).zip_list) == 20


def test_every_plan_keeps_77_percent():
    # Worst case: the whole allowance used, plus card fees (3.6% + $0.30) and ~$2.50 hosting.
    for tier in ("personal", "pro", "premium"):
        p = plans.PLANS[tier]
        kept = p["price"] - p["allowance_usd"] - (p["price"] * 0.036 + 0.30) - 2.50
        assert kept / p["price"] >= 0.77, tier
    prem = plans.PLANS["premium"]
    assert prem["max_zips"] * prem["max_industries"] * plans.SEARCH_COST <= prem["allowance_usd"]
