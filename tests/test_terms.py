"""The agreement box: sign-up needs it ticked, the date is saved, and earlier users tick it once."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, gmail
from app.db import engine
from app.main import app
from app.models import Account, Organization, User
from tests.conftest import login_as


def test_signup_needs_the_box_ticked(monkeypatch):
    monkeypatch.setattr(gmail, "configured", lambda: True)
    monkeypatch.setattr(auth, "start_login", lambda company: "https://accounts.google.test/")
    c = TestClient(app, follow_redirects=False)
    assert "not affiliated with or endorsed by N2" in c.get("/signup").text
    assert c.get("/auth/google?company=Test:%20Terms%20Co").headers["location"].startswith("/signup?msg=Please%20tick")
    assert c.get("/auth/google?company=Test:%20Terms%20Co&offer=free").headers["location"].startswith("/try?msg=")
    assert c.get("/auth/google?company=Test:%20Terms%20Co&agree=yes").headers["location"] == "https://accounts.google.test/"


def test_signup_saves_when_they_agreed(monkeypatch):
    monkeypatch.setattr(auth, "REQUIRE_TERMS", True)
    monkeypatch.setattr(gmail, "configured", lambda: True)
    monkeypatch.setattr(auth, "start_login", lambda company: "https://accounts.google.test/")
    monkeypatch.setattr(auth, "google_identity", lambda s, c: ({"email": "new@terms.test", "name": "Terms Person"}, "Test: Terms Co"))
    c = TestClient(app, follow_redirects=False)
    c.get("/auth/google?company=Test:%20Terms%20Co&agree=yes")
    assert c.get("/auth/callback?state=s&code=c").headers["location"] == "/setup"
    assert c.get("/setup").status_code == 200  # no second ask
    with Session(engine) as db:
        u = db.scalars(select(User).where(User.email == "new@terms.test")).one()
        assert u.terms_agreed_at and u.terms_version == auth.TERMS_VERSION
        db.delete(u)
        db.commit()


def test_earlier_users_tick_it_once(monkeypatch):
    monkeypatch.setattr(auth, "REQUIRE_TERMS", True)
    with Session(engine) as db:
        org = Organization(name="Test: terms gate")
        db.add(org)
        db.flush()
        acct = Account(org_id=org.id, name="Test: terms gate", target_zips="37934", industries="Dentist")
        db.add(acct)
        db.commit()
        acct_id = acct.id
    c = TestClient(app, follow_redirects=False)
    uid = login_as(c, acct_id)
    assert c.get("/leads").headers["location"] == "/agree"
    assert c.get("/leads", headers={"HX-Request": "true"}).headers["HX-Redirect"] == "/agree"
    assert "One quick thing" in c.get("/agree").text
    assert c.post("/agree").headers["location"].startswith("/agree?msg=")
    assert c.post("/agree", data={"agree": "yes"}).headers["location"] == "/leads"
    assert c.get("/leads").status_code == 200
    with Session(engine) as db:
        assert db.get(User, uid).terms_version == auth.TERMS_VERSION
