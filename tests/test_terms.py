"""The agreement box: sign-up needs it ticked and the date is saved."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, gmail
from app.db import engine
from app.main import app
from app.models import User


def test_signup_needs_the_box_ticked(monkeypatch):
    monkeypatch.setattr(gmail, "configured", lambda: True)
    monkeypatch.setattr(auth, "start_login", lambda company: "https://accounts.google.test/")
    c = TestClient(app, follow_redirects=False)
    assert "not affiliated with or endorsed by N2" in c.get("/signup").text
    assert c.get("/auth/google?company=Test:%20Terms%20Co").headers["location"].startswith("/signup?msg=Please%20tick")
    assert c.get("/auth/google?company=Test:%20Terms%20Co&offer=free").headers["location"].startswith("/try?msg=")
    assert c.get("/auth/google?company=Test:%20Terms%20Co&agree=yes").headers["location"] == "https://accounts.google.test/"


def test_signup_saves_when_they_agreed(monkeypatch):
    monkeypatch.setattr(gmail, "configured", lambda: True)
    monkeypatch.setattr(auth, "start_login", lambda company: "https://accounts.google.test/")
    monkeypatch.setattr(auth, "google_identity", lambda s, c: ({"email": "new@terms.test", "name": "Terms Person"}, "Test: Terms Co"))
    c = TestClient(app, follow_redirects=False)
    c.get("/auth/google?company=Test:%20Terms%20Co&agree=yes")
    assert c.get("/auth/callback?state=s&code=c").headers["location"] == "/setup"
    with Session(engine) as db:
        u = db.scalars(select(User).where(User.email == "new@terms.test")).one()
        assert u.terms_agreed_at and u.terms_version == auth.TERMS_VERSION
        db.delete(u)
        db.commit()

