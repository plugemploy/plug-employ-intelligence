"""Checks sign-in, sign-up, invites and that companies can never see each other's data."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import auth, close_crm
from app.db import engine
from app.main import app
from app.models import Account, Feedback, Lead, Organization, User
from tests.conftest import login_as


@pytest.fixture
def two_companies():
    with Session(engine) as db:
        o1, o2 = Organization(name="Test: Alpha Media"), Organization(name="Test: Beta Media")
        db.add_all([o1, o2])
        db.flush()
        a1 = auth.new_account(db, o1.id, "Test: Alpha North")
        a2 = auth.new_account(db, o1.id, "Test: Alpha South")
        b1 = auth.new_account(db, o2.id, "Test: Beta One")
        db.add_all([Lead(account_id=a1.id, business_name="North Lead"), Lead(account_id=a2.id, business_name="South Lead"),
                    Lead(account_id=b1.id, business_name="Beta Secret")])
        from datetime import datetime, timezone
        for a in (a1, a2, b1):
            a.setup_done_at = datetime.now(timezone.utc)
        owner = User(org_id=o1.id, account_id=a1.id, email="owner@alpha.test", role="owner")
        pub = User(org_id=o1.id, account_id=a2.id, email="pub@alpha.test", role="publisher")
        db.add_all([owner, pub])
        db.commit()
        out = dict(o1=o1.id, a1=a1.id, a2=a2.id, b1=b1.id, owner=owner.id, pub=pub.id)
    yield out
    with Session(engine) as db:
        db.execute(delete(Organization).where(Organization.name.like("Test:%")))
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.execute(delete(User).where(User.email.like("%.test")))
        db.commit()


def client_as(user_id):
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, auth.make_session(user_id))
    return c


def test_must_sign_in():
    c = TestClient(app)
    r = c.get("/leads", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert TestClient(app).get("/login").status_code == 200
    bad = TestClient(app)
    bad.cookies.set(auth.SESSION_COOKIE, "forged")
    assert bad.get("/leads", follow_redirects=False).headers["location"] == "/login"


def test_publisher_sees_only_own_account(two_companies):
    c = client_as(two_companies["pub"])
    html = c.get("/leads").text
    assert "South Lead" in html and "North Lead" not in html and "Beta Secret" not in html
    assert c.post("/switch-account", data={"account_id": two_companies["a1"]}).status_code == 404
    c.cookies.set("account_id", str(two_companies["a1"]))  # tampered cookie is ignored
    assert "North Lead" not in c.get("/leads").text
    assert c.get("/admin", follow_redirects=False).status_code == 303


def test_admin_switches_within_company_only(two_companies):
    c = client_as(two_companies["owner"])
    assert c.post("/switch-account", data={"account_id": two_companies["a2"]}).status_code == 200
    assert "South Lead" in c.get("/leads").text
    assert c.post("/switch-account", data={"account_id": two_companies["b1"]}).status_code == 404
    c.cookies.set("account_id", str(two_companies["b1"]))
    assert "Beta Secret" not in c.get("/leads").text
    admin = c.get("/admin").text
    assert "Test: Alpha South" in admin and "Test: Beta One" not in admin


def test_invite_and_sign_up(two_companies):
    c = client_as(two_companies["owner"])
    c.post("/admin/publishers", data={"name": "Test: New Pub", "email": "New@Alpha.test", "zips": "37934, 999"})
    with Session(engine) as db:
        u = auth.sign_in(db, "new@alpha.test", "New Pub")
        acct = db.get(Account, u.account_id)
        assert (u.org_id, u.role, acct.zip_list) == (two_companies["o1"], "publisher", ["37934"])
        with pytest.raises(ValueError):
            auth.sign_in(db, "stranger@nowhere.test", "X")
        owner = auth.sign_in(db, "founder@gamma.test", "Gia", signup_company="Test: Gamma Living")
        org = db.get(Organization, owner.org_id)
        assert (org.name, org.plan, owner.role) == ("Test: Gamma Living", "trial", "owner") and owner.account_id


def test_owner_close_key_only_for_owner_company(two_companies, monkeypatch):
    monkeypatch.setattr(close_crm, "CLOSE_API_KEY", "api_owner_env")
    with Session(engine) as db:
        assert close_crm.key_for(db, db.get(Account, two_companies["a1"])) == ""
        monkeypatch.setattr(close_crm, "OWNER_ORG", "Test: Alpha Media")
        assert close_crm.key_for(db, db.get(Account, two_companies["a1"])) == "api_owner_env"
        assert close_crm.key_for(db, db.get(Account, two_companies["b1"])) == ""


def test_feedback_box_and_owner_page():
    with Session(engine) as db:
        a = Account(name="Test: Feedback Pub")
        db.add(a)
        db.commit()
        aid = a.id
    c = TestClient(app)
    login_as(c, aid)
    assert "Send feedback" in c.get("/leads").text
    assert "Thanks!" in c.post("/feedback", data={"text": "Test: the search page confused me", "page": "/search"}).text
    assert "Type something" in c.post("/feedback", data={"text": "  "}).text
    assert c.get("/feedback").status_code == 404  # testers can't read everyone's feedback
    with Session(engine) as db:
        f = db.scalars(select(Feedback).where(Feedback.account_id == aid)).one()
        assert (f.page, f.text) == ("/search", "Test: the search page confused me")
        db.delete(f)
        db.commit()


def test_results_pages_and_beta_dashboard():
    from datetime import datetime, timezone
    from app.models import FollowUp
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        owner_org = Organization(name="Test: Owner Co", plan="active", plan_tier="owner")
        tester_org = Organization(name="Test: Tester Co", plan="trial", plan_tier="beta")
        db.add_all([owner_org, tester_org])
        db.flush()
        mine = Account(name="Test: Owner Acct", org_id=owner_org.id, setup_done_at=now)
        theirs = Account(name="Test: Tester Acct", org_id=tester_org.id, setup_done_at=now)
        db.add_all([mine, theirs])
        db.flush()
        db.add_all([
            Lead(account_id=theirs.id, business_name="Hot New", score=9),
            Lead(account_id=theirs.id, business_name="Called", status="Contacted", contacted_at=now),
            Lead(account_id=theirs.id, business_name="Booked", status="Appointment set", contacted_at=now, appt_set_at=now),
            Lead(account_id=theirs.id, business_name="Closed One", status="Sold", contacted_at=now, appt_set_at=now,
                 appt_shown_at=now, sold_at=now, mrr=300, contract_months=12, ad_mgmt_fee=500),
            Lead(account_id=theirs.id, business_name="No Show", status="Not interested", contacted_at=now, appt_set_at=now),
            Lead(account_id=theirs.id, business_name="Deleted Win", status="Sold", sold_at=now, mrr=9999, deleted_at=now),
        ])
        db.add(User(org_id=tester_org.id, account_id=theirs.id, email="tester@results.test", role="owner",
                    last_login_at=now))
        db.commit()
        mine_id, theirs_id = mine.id, theirs.id
    tester = TestClient(app)
    login_as(tester, theirs_id)
    html = tester.get("/results").text
    assert "$3,600" in html and "$500" in html and "$300" in html  # contract value, ad fee, MRR; deleted sale not counted
    assert tester.get("/beta").status_code == 404 and tester.get("/beta.csv").status_code == 404
    owner = TestClient(app)
    login_as(owner, mine_id)
    html = owner.get("/beta").text
    assert "Test: Tester Co" in html and "tester@results.test" in html and "Test: Owner Co" not in html
    csv_text = owner.get("/beta.csv").text
    line = [l for l in csv_text.splitlines() if l.startswith("Test: Tester Co")][0]
    # contacted, set, shown, sold, MRR, contract value, ad fees, found only via app, feedback, then 5 empty survey cells
    assert line.endswith(",4,3,1,1,300,3600,500,0,0,,,,,")



def test_beta_survey_due_saved_and_reported():
    from datetime import datetime, timedelta, timezone
    from app.models import BetaSurvey
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        owner_org = Organization(name="Test: Survey Owner", plan="active", plan_tier="owner")
        old_org = Organization(name="Test: Day 26 Co", plan="trial", plan_tier="beta", created_at=now - timedelta(days=26))
        new_org = Organization(name="Test: Day 5 Co", plan="trial", plan_tier="beta", created_at=now - timedelta(days=5))
        db.add_all([owner_org, old_org, new_org])
        db.flush()
        accts = [Account(name=f"Test: {o.name}", org_id=o.id, setup_done_at=now) for o in (owner_org, old_org, new_org)]
        db.add_all(accts)
        db.flush()
        db.add(Lead(account_id=accts[1].id, business_name="Only Here", status="Sold", sold_at=now, mrr=200,
                    contract_months=6, found_elsewhere="no"))
        db.commit()
        mine, old_id, new_id = (a.id for a in accts)
    c_old, c_new = TestClient(app), TestClient(app)
    login_as(c_old, old_id)
    login_as(c_new, new_id)
    assert "Your beta is almost over" in c_old.get("/leads").text
    assert "Your beta is almost over" not in c_new.get("/leads").text  # too early
    later = c_old.post("/survey/later")
    assert "survey_later" in later.headers.get("set-cookie", "")
    c_old.cookies.clear()
    login_as(c_old, old_id)
    r = c_old.post("/survey", data={"would_pay": "pro", "recommend": "9", "hours_saved": "3.5",
                                    "quote": "Booked 4 appointments my first week.", "quote_ok": "1", "next": "/dashboard"},
                   follow_redirects=False)
    assert r.headers["location"].startswith("/dashboard?msg=")
    assert "Your beta is almost over" not in c_old.get("/leads").text  # answered: never again
    owner = TestClient(app)
    login_as(owner, mine)
    html = owner.get("/beta").text
    assert "Booked 4 appointments my first week." in html and "OK to quote" in html
    assert "Yes, Pro ($149)" in html and "Recommend 9/10" in html
    line = [l for l in owner.get("/beta.csv").text.splitlines() if l.startswith("Test: Day 26 Co")][0]
    assert line.endswith(',1,0,"Yes, Pro ($149)",9,3.5,Booked 4 appointments my first week.,yes')  # found only here, feedback, survey
    assert "<b>1</b> of your 1 sale" in c_old.get("/results").text
