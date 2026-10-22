"""Checks the My business page and the Gmail check with fake Gmail (no network, no cost)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tests.conftest import login_as
from app import gmail
from app.db import engine
from app.main import app
from app.models import Account, FollowUp, Lead

NOW = datetime.now(timezone.utc)
FAKE_INBOX = {"farragutdental.com": NOW - timedelta(days=30), "recent.com": NOW - timedelta(days=2),
              "wonclient.com": NOW - timedelta(days=60)}


@pytest.fixture
def ids():
    with Session(engine) as db:
        a, b = Account(name="Test: Biz A", gmail_token_enc="x"), Account(name="Test: Biz B")
        db.add_all([a, b])
        db.flush()
        db.add_all([
            Lead(account_id=a.id, business_name="Farragut Dental", website="https://www.farragutdental.com/"),
            Lead(account_id=a.id, business_name="Recent Co", website="https://recent.com"),
            Lead(account_id=a.id, business_name="Won Client", website="https://wonclient.com", status="Won"),
            Lead(account_id=a.id, business_name="Never Emailed", website="https://quiet.com"),
            Lead(account_id=a.id, business_name="Gmail Site", website="https://gmail.com"),
            Lead(account_id=b.id, business_name="Farragut Dental", website="https://farragutdental.com"),
        ])
        db.commit()
        out = {"a": a.id, "b": b.id}
    yield out
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


@pytest.fixture
def fake_gmail(monkeypatch):
    asked = []
    monkeypatch.setattr(gmail, "configured", lambda: True)
    monkeypatch.setattr(gmail, "gmail_service", lambda db, account: "fake")
    monkeypatch.setattr(gmail, "latest_email_time", lambda svc, d: asked.append(d) or FAKE_INBOX.get(d))
    return asked


def test_check_tags_and_follow_ups(ids, fake_gmail):
    with Session(engine) as db:
        s = gmail.check_account(db, ids["a"])
        assert s == {"checked": 4, "emailed": 3, "follow_ups": 1}
        assert "gmail.com" not in fake_gmail  # free mail never searched
        leads = {l.business_name: l for l in db.scalars(select(Lead).where(Lead.account_id == ids["a"]))}
        assert leads["Farragut Dental"].emailed_domain == "farragutdental.com"
        assert leads["Never Emailed"].last_emailed_at is None
        fus = db.scalars(select(FollowUp).where(FollowUp.account_id == ids["a"])).all()
        assert [f.lead_id for f in fus] == [leads["Farragut Dental"].id]  # not Recent (2 days), not Won
        assert gmail.check_account(db, ids["a"])["follow_ups"] == 0  # no duplicate follow-up
        # account B has no Gmail connected: untouched
        assert gmail.check_account(db, ids["b"]) == {"checked": 0, "emailed": 0, "follow_ups": 0}


def test_site_domain():
    assert gmail.site_domain("https://www.Example.com/path") == "example.com"
    assert gmail.site_domain("example.org") == "example.org"


def test_pages(ids, fake_gmail):
    c = TestClient(app)
    login_as(c, ids["a"])
    r = c.post("/business", data={"description": "We sell billboards.", "zips": "37919, 37934\n12, 37919",
                                  "industries": ["Dentist", "Golf"], "extra_industries": "Florist, Dentist"})
    assert r.status_code == 200
    with Session(engine) as db:
        a = db.get(Account, ids["a"])
        assert (a.business_description, a.zip_list, a.industry_list) == (
            "We sell billboards.", ["37919", "37934"], ["Dentist", "Golf", "Florist"])
    assert "emailed 3 leads" in c.post("/gmail/check").text
    assert "Emailed ·" in c.get("/leads").text
    assert "Farragut Dental" not in c.get("/leads?hide_contacted=1").text
    assert "Follow up: last emailed" in c.get("/due").text
    assert "disconnected" in c.post("/gmail/disconnect").text
    with Session(engine) as db:
        assert db.get(Account, ids["a"]).gmail_token_enc == ""


def test_token_is_encrypted(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(gmail, "APP_SECRET_KEY", Fernet.generate_key().decode())
    secret = gmail._fernet().encrypt(b'{"refresh_token": "abc"}').decode()
    assert "abc" not in secret and gmail._fernet().decrypt(secret.encode()) == b'{"refresh_token": "abc"}'
