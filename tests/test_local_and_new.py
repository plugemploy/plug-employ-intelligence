"""Checks local pages to watch and New in your territory (fake pages, fake Overture, fake Claude)."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import local_pages, new_in_area, overture
from app.db import engine
from app.main import app
from app.models import Account, Lead, LeadSignal, NewInAreaCheck, OvertureSnapshot, WatchedPage
from tests.conftest import login_as, on_plan

TODAY = date.today()


@pytest.fixture
def acct():
    with Session(engine) as db:
        a = Account(name="Test: Local", target_zips="37027", industries="Med spa,Dentist",
                    setup_done_at=datetime.now(timezone.utc))
        db.add(a)
        db.flush()
        db.add(Lead(account_id=a.id, business_name="Brentwood Smiles", zip_code="37027", phone="615-555-0100"))
        db.commit()
        aid = a.id
    on_plan(aid, "pro")
    yield aid
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.execute(delete(OvertureSnapshot).where(OvertureSnapshot.zip_code == "37027"))
        db.commit()


def mention(name, kind="ribbon_cutting", text="Ribbon cutting with the Chamber.", when="", phone="", zip_code=""):
    return local_pages.Mention(name=name, kind=kind, text=text, date=when, address="", zip_code=zip_code,
                               phone=phone, website="")


def test_watched_pages_saved_from_my_business(acct):
    c = TestClient(app)
    login_as(c, acct)
    c.post("/business", data={"description": "x", "zips": "37027", "industries": ["Med spa"],
                              "watch_pages": "brentwoodchamber.com/ribbon-cuttings\nhttps://news.test/new-biz\n\n"})
    with Session(engine) as db:
        urls = db.scalars(select(WatchedPage.url).where(WatchedPage.account_id == acct).order_by(WatchedPage.id)).all()
    assert urls == ["https://brentwoodchamber.com/ribbon-cuttings", "https://news.test/new-biz"]
    assert "Check my local pages now" in c.get("/business").text


def test_page_matches_leads_and_adds_new_ones(acct, monkeypatch):
    monkeypatch.setattr(local_pages, "fetch_page", lambda url: "Ribbon cuttings this month ...")
    monkeypatch.setattr(local_pages, "ask_claude", lambda text, today: ([
        mention("Brentwood Smiles", phone="(615) 555-0100"),                            # already a lead
        mention("Glow Med Spa", kind="opening", text="Grand opening Nov 2.", zip_code="37027",
                when=(TODAY + timedelta(days=10)).isoformat()),                         # new lead
        mention("Old News Cafe", when=(TODAY - timedelta(days=200)).isoformat()),       # too old
    ], 0.001))
    with Session(engine) as db:
        local_pages.set_pages(db, acct, ["https://chamber.test/ribbons"])
        s = local_pages.check_account(db, acct)
        assert (s["pages"], s["added"], s["matched"]) == (1, 1, 1)
        names = set(db.scalars(select(Lead.business_name).where(Lead.account_id == acct)))
        assert names == {"Brentwood Smiles", "Glow Med Spa"}
        kinds = {(l.business_name, s.kind) for s in db.scalars(select(LeadSignal).where(LeadSignal.account_id == acct))
                 for l in [s.lead]}
        assert kinds == {("Brentwood Smiles", "ribbon_cutting"), ("Glow Med Spa", "opening")}
        page = db.scalars(select(WatchedPage).where(WatchedPage.account_id == acct)).one()
        assert page.last_status.startswith("ok: 2 businesses")
        assert local_pages.check_account(db, acct)["pages"] == 0  # read this week already


def place(pid, name, category="medical_spa"):
    return dict(id=pid, name=name, category=category, basic_category="wellness_service", confidence=0.9,
                operating_status="open", phones=[f"+1615555{abs(hash(pid)) % 9000 + 1000}"], websites=[],
                emails=[], brand="", address="1 Main St, Brentwood, TN 37027")


def test_new_in_area_baseline_then_new_businesses(acct, monkeypatch):
    month1 = [place("a", "Old Spa"), place("b", "Old Dental", "dental_clinic")]
    month2 = month1 + [place("c", "Brand New Spa"), place("d", "New Pizza", "pizza_restaurant")]
    current = {"places": month1, "release": "2026-09-23.0"}
    monkeypatch.setattr(overture, "fetch_places", lambda z: current["places"])
    monkeypatch.setattr(overture, "latest_release", lambda: current["release"])
    with Session(engine) as db:
        a = db.get(Account, acct)
        assert new_in_area.check_account(db, a)["added"] == 0  # first month: baseline only
        assert new_in_area.check_account(db, a)["zips"] == 0    # same release: nothing to do
        current.update(places=month2, release="2026-10-21.0")
        assert new_in_area.check_account(db, a)["added"] == 1   # the spa; pizza isn't one of their types
        lead = db.scalars(select(Lead).where(Lead.account_id == acct, Lead.business_name == "Brand New Spa")).one()
        sig = db.scalars(select(LeadSignal).where(LeadSignal.lead_id == lead.id)).one()
        assert (lead.industry, sig.kind, sig.text) == ("Med spa", "new_in_area", "Newly listed in 37027 this month.")
        assert db.scalar(select(NewInAreaCheck.release).where(NewInAreaCheck.account_id == acct)) == "2026-10-21.0"
