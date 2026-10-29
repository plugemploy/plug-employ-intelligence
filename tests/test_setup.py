"""Checks the setup wizard and industry picker (fake Claude, fake search; no cost)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import auth, catalog, setup
from app.db import engine
from app.main import app
from app.models import Account, NightlyRun, Organization, User


@pytest.fixture
def newbie():
    with Session(engine) as db:
        org = Organization(name="Test: Setup Co")
        db.add(org)
        db.flush()
        acct = auth.new_account(db, org.id, "Test: Setup Pub", "37934")
        user = User(org_id=org.id, account_id=acct.id, email="new@setup.test", role="owner")
        db.add(user)
        db.commit()
        out = {"acct": acct.id, "user": user.id}
    yield out
    with Session(engine) as db:
        db.execute(delete(Organization).where(Organization.name.like("Test:%")))
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def client(uid):
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.make_session(uid))
    return c


def test_new_account_goes_through_setup(newbie, monkeypatch):
    c = client(newbie["user"])
    r = c.get("/leads", follow_redirects=False)
    assert r.headers["location"] == "/setup"
    page = c.get("/setup").text
    assert "Select all" in page and "Florist" in page and "37934" in page

    monkeypatch.setattr(setup, "ask_claude", lambda d: (["Florist", "wedding venue", "Balloon artist, LLC"], 0.001))
    monkeypatch.setattr(setup, "ANTHROPIC_API_KEY", "x")
    picker = c.post("/setup/suggest", data={"description": "Wedding magazine", "zips": "37934"}).text
    assert 'value="Florist" checked' in picker and 'value="Wedding venue" checked' in picker
    assert 'value="Balloon artist LLC" checked' in picker  # custom kept, comma removed

    assert 'name="company"' in page
    r = c.post("/setup", data={"description": "Wedding magazine", "zips": "37934, 37919", "company": "Test: Bride Mag",
                               "industries": ["Florist", "Wedding venue", "Balloon artist LLC"]}, follow_redirects=False)
    assert r.headers["location"] == "/setup/first"
    with Session(engine) as db:
        a = db.get(Account, newbie["acct"])
        assert db.get(Organization, a.org_id).name == "Test: Bride Mag"
        assert a.setup_done_at and a.zip_list == ["37934", "37919"] and len(a.industry_list) == 3
    assert "6</b> searches in total" in c.get("/setup/first").text
    assert c.get("/leads", follow_redirects=False).status_code == 200


def test_setup_requires_zip_and_industry(newbie):
    c = client(newbie["user"])
    r = c.post("/setup", data={"description": "x", "zips": "", "industries": []}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    with Session(engine) as db:
        assert db.get(Account, newbie["acct"]).setup_done_at is None


def test_starter_search(newbie, monkeypatch):
    from app import nightly
    monkeypatch.setattr(nightly, "run_for_account",
                        lambda aid, max_searches=None: NightlyRun(account_id=aid, searches=max_searches, added=7, high_scores=3, error=""))
    with Session(engine) as db:
        setup.finish(db, db.get(Account, newbie["acct"]))
    r = client(newbie["user"]).post("/setup/first")
    assert "found 7 leads; 3 scored 8+" in r.text


def test_catalog_has_no_duplicates():
    assert len(catalog.ALL) == len(set(catalog.ALL)) and len(catalog.ALL) > 100


def test_finding_leads_popup_counts(newbie):
    from datetime import datetime, timezone
    from app.models import ApiUsage, Lead
    with Session(engine) as db:
        a = db.get(Account, newbie["acct"])
        setup.finish(db, a)
        db.add(Lead(account_id=a.id, business_name="Test Old Lead"))  # from before: not counted
        db.commit()
    c = client(newbie["user"])
    page = c.get("/setup/first").text
    assert 'id="finding"' in page and 'onsubmit="startFinding(this)"' in page
    import re
    after_lead = int(re.search(r"after_lead=(\d+)", page).group(1))
    after_usage = int(re.search(r"after_usage=(\d+)", page).group(1))
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        db.add(ApiUsage(account_id=newbie["acct"], service="overture_search", detail="Dentist in 37934", est_cost_usd=0))
        db.add_all([Lead(account_id=newbie["acct"], business_name="Test New A", website_checked_at=now, score=9),
                    Lead(account_id=newbie["acct"], business_name="Test New B", website_checked_at=now),
                    Lead(account_id=newbie["acct"], business_name="Test New C")])
        db.commit()
    p = c.get(f"/setup/progress?after_lead={after_lead}&after_usage={after_usage}").json()
    assert p == {"searches": 1, "found": 3, "read": 2, "scored": 1, "high": 1}
