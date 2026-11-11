"""Checks the Foursquare second opinion (fake places, no network)."""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import foursquare, overture
from app.db import engine
from app.models import Account, Lead
from tests.conftest import on_plan
from tests.test_overture import place

FSQ = [
    dict(name="Gone Dental", phone="8655550201", website="", closed=True),
    dict(name="Open Dental", phone="8655550299", website="", closed=False),  # a different number than Overture's
    dict(name="Same Dental", phone="8655550203", website="", closed=False),
]


@pytest.fixture
def acct():
    with Session(engine) as db:
        a = Account(name="Test: Foursquare")
        db.add(a)
        db.commit()
        aid = a.id
    on_plan(aid, "pro")
    yield aid
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def test_search_skips_closed_and_adds_foursquares_number(acct, monkeypatch):
    monkeypatch.setattr(foursquare, "fetch_zip", lambda z: FSQ)
    monkeypatch.setattr(overture, "fetch_places", lambda z: [
        place("o1", "Gone Dental", phones=("+18655550201",), websites=("https://gonedental.test",)),
        place("o2", "Open Dental", phones=("+18655550202",), websites=("https://opendental.test",)),
        place("o3", "Same Dental", phones=("+18655550203",), websites=("https://samedental.test",)),
        place("o4", "Unknown Dental", phones=("+18655550204",), websites=("https://unknowndental.test",)),  # not in Foursquare: kept as before
    ])
    with Session(engine) as db:
        s = overture.search_and_save(db, acct, "37934", "Dentist")
        assert (s["added"], s["skipped_closed"]) == (3, 1)
        leads = {l.business_name: l for l in db.scalars(select(Lead).where(Lead.account_id == acct))}
        assert "Gone Dental" not in leads
        assert (leads["Open Dental"].phone, leads["Open Dental"].other_phone, leads["Open Dental"].other_phone_source) == \
            ("+18655550202", "+18655550299", "foursquare")
        assert leads["Same Dental"].other_phone == "" and leads["Same Dental"].fsq_checked_at
        assert leads["Unknown Dental"].other_phone == ""


def test_search_carries_on_when_foursquare_cant_be_read(acct, monkeypatch):
    monkeypatch.setattr(overture, "fetch_places", lambda z: [place("o1", "Gone Dental", phones=("+18655550201",))])
    with Session(engine) as db:  # conftest: fetch_zip returns None
        assert overture.search_and_save(db, acct, "37934", "Dentist")["added"] == 1


def test_monthly_check_of_existing_leads(acct, monkeypatch):
    monkeypatch.setattr(foursquare, "configured", lambda: True)
    monkeypatch.setattr(foursquare, "fetch_zip", lambda z: FSQ)
    with Session(engine) as db:
        gone = Lead(account_id=acct, business_name="Gone Dental", zip_code="37934", phone="+18655550201")
        called = Lead(account_id=acct, business_name="Gone Dental", zip_code="37934", phone="", status="Contacted")
        other = Lead(account_id=acct, business_name="Open Dental", zip_code="37934", phone="+18655550202")
        recent = Lead(account_id=acct, business_name="Open Dental", zip_code="37934", phone="+18655550202",
                      fsq_checked_at=datetime.now(timezone.utc) - timedelta(days=3))
        db.add_all([gone, called, other, recent])
        db.commit()
        stats = foursquare.check_account(db, acct)
        assert stats["checked"] == 3  # not the one checked 3 days ago
        assert gone.deleted_at and gone.closed_reason == foursquare.REASON  # untouched: removed
        assert not called.deleted_at and called.closed_reason == foursquare.REASON  # being worked: flagged
        assert other.other_phone == "+18655550299" and not other.deleted_at
        assert recent.other_phone == ""


def _token(exp: datetime) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": int(exp.timestamp())}).encode()).decode().rstrip("=")
    return f"x.{body}.y"


def test_owner_is_warned_before_the_key_expires(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(foursquare, "FOURSQUARE_TOKEN", _token(now + timedelta(days=20)))
    assert foursquare.token_status() == ""
    monkeypatch.setattr(foursquare, "FOURSQUARE_TOKEN", _token(now + timedelta(days=3)))
    assert "expires on" in foursquare.token_status()
    monkeypatch.setattr(foursquare, "FOURSQUARE_TOKEN", _token(now - timedelta(days=1)))
    assert "expired on" in foursquare.token_status()
