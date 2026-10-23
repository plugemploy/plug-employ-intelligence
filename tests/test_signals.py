"""Checks reasons to reach out (fake websites and fake Claude answers, no network)."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import dashboard, signals, website
from app.db import engine
from app.main import app
from app.models import Account, Lead, LeadSignal
from tests.conftest import login_as, on_plan

TODAY = date.today()


@pytest.fixture
def ids():
    with Session(engine) as db:
        a = Account(name="Test: Signals")
        db.add(a)
        db.flush()
        spa = Lead(account_id=a.id, business_name="Glow Spa", website="https://glow.test", score=9, industry="Med spa")
        old = Lead(account_id=a.id, business_name="Fresh Check", website="https://fresh.test", score=5,
                   signals_checked_at=datetime.now(timezone.utc))
        sold = Lead(account_id=a.id, business_name="Client Spa", website="https://client.test", score=8,
                    industry="Med spa", status="Sold")
        new_spa = Lead(account_id=a.id, business_name="Next Spa", score=7, industry="Med spa")
        db.add_all([spa, old, sold, new_spa])
        db.commit()
        out = {"a": a.id, "spa": spa.id, "old": old.id, "sold": sold.id, "next": new_spa.id}
    on_plan(out["a"], "pro")
    yield out
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def fake_reasons(*items):
    return [signals.Reason(kind=k, text=t, date=d) for k, t, d in items]


def test_check_saves_timely_reasons_once(ids, monkeypatch):
    monkeypatch.setattr(website, "fetch_news", lambda url: ("Grand opening of our Farragut location!", "https://glow.test/news"))
    soon = (TODAY + timedelta(days=10)).isoformat()
    stale = (TODAY - timedelta(days=200)).isoformat()
    monkeypatch.setattr(signals, "ask_claude", lambda lead, text, today: (fake_reasons(
        ("new_location", "Opening a second location in Farragut.", soon),
        ("award", "Best Med Spa 2024.", stale),            # too old: skipped
        ("hiring", "Hiring two aestheticians.", "")), 0.0002))
    with Session(engine) as db:
        due = [l.business_name for l in signals.due_leads(db, ids["a"])]
        assert due[0] == "Glow Spa" and "Fresh Check" not in due  # best first; recently checked skipped
        stats = signals.check_account(db, ids["a"])
        assert stats["found"] >= 2
        sigs = db.scalars(select(LeadSignal).where(LeadSignal.lead_id == ids["spa"])).all()
        assert {s.kind for s in sigs} == {"new_location", "hiring"}
        assert next(s for s in sigs if s.kind == "new_location").event_date == TODAY + timedelta(days=10)
        assert sigs[0].source_url == "https://glow.test/news"
        lead = db.get(Lead, ids["spa"])
        lead.signals_checked_at = None
        db.commit()
        assert signals.check_lead(db, lead) == 0  # same news again: not repeated


def test_dashboard_lead_page_list_and_done(ids):
    with Session(engine) as db:
        db.add(LeadSignal(account_id=ids["a"], lead_id=ids["spa"], kind="event", text="Hosting a ribbon cutting.",
                          event_date=TODAY + timedelta(days=3)))
        db.add(LeadSignal(account_id=ids["a"], lead_id=ids["spa"], kind="promotion", text="Old promo.",
                          event_date=TODAY - timedelta(days=40)))  # past: hidden
        db.commit()
    c = TestClient(app)
    login_as(c, ids["a"])
    page = c.get("/dashboard").text
    assert "Hosting a ribbon cutting." in page and "Old promo." not in page
    assert "Glow Spa" in c.get(f"/leads/{ids['spa']}").text and "Reasons to reach out" in c.get(f"/leads/{ids['spa']}").text
    listing = c.get("/leads?reasons=1").text
    assert "Glow Spa" in listing and "🔔 1" in listing and "Fresh Check" not in listing
    with Session(engine) as db:
        sid = db.scalars(select(LeadSignal.id).where(LeadSignal.kind == "event", LeadSignal.account_id == ids["a"])).one()
    assert c.post(f"/signals/{sid}/done").status_code == 200
    assert "Hosting a ribbon cutting." not in c.get("/dashboard").text


def test_like_your_customers(ids):
    with Session(engine) as db:
        groups = dashboard.like_your_customers(db, ids["a"])
    assert groups[0]["industry"] == "Med spa" and groups[0]["sold"] == 1 and groups[0]["waiting"] == 2
    assert [l.business_name for l in groups[0]["top"]] == ["Glow Spa", "Next Spa"]
    c = TestClient(app)
    login_as(c, ids["a"])
    assert "Like your customers" in c.get("/dashboard").text


def test_cannot_dismiss_another_accounts_reason(ids):
    with Session(engine) as db:
        other = Account(name="Test: Other Signals")
        db.add(other)
        db.flush()
        sig = LeadSignal(account_id=ids["a"], lead_id=ids["spa"], kind="hiring", text="Hiring.")
        db.add(sig)
        db.commit()
        other_id, sid = other.id, sig.id
    c = TestClient(app)
    login_as(c, other_id)
    assert c.post(f"/signals/{sid}/done").status_code == 404


def test_past_events_and_untimely_items_are_dropped(ids, monkeypatch):
    monkeypatch.setattr(website, "fetch_news", lambda url: ("text", ""))
    monkeypatch.setattr(signals, "ask_claude", lambda lead, text, today: ([
        signals.Reason(kind="event", text="Fall gala.", date=(TODAY - timedelta(days=5)).isoformat()),
        signals.Reason(kind="event", text="JUVEDERM Day, already past.", date="", timely=False),
        signals.Reason(kind="event", text="Ladies Night.", date=(TODAY + timedelta(days=5)).isoformat()),
    ], 0.0001))
    with Session(engine) as db:
        assert signals.check_lead(db, db.get(Lead, ids["spa"])) == 1
        assert db.scalars(select(LeadSignal.text).where(LeadSignal.lead_id == ids["spa"])).all() == ["Ladies Night."]
