"""Checks the Overture lead search with made-up places (no network)."""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import overture, overture_types, places
from app.db import engine
from app.models import Account, ApiUsage, Lead
from tests.conftest import on_plan


def place(pid, name, category="dental_clinic", basic="dental_clinic", confidence=0.9, status="open",
          phones=("+18655550100",), websites=("https://example.com",), emails=(), brand=""):
    return dict(id=pid, name=name, category=category, basic_category=basic, confidence=confidence,
                operating_status=status, phones=list(phones), websites=list(websites), emails=list(emails),
                brand=brand, address="100 Main St, Knoxville, TN 37934")


@pytest.fixture
def acct():
    with Session(engine) as db:
        a = Account(name="Test: Overture")
        db.add(a)
        db.flush()
        db.add(Lead(account_id=a.id, business_name="Old Google Dental", zip_code="37934", phone="(865) 555-0199",
                    google_place_id="g1"))
        db.commit()
        aid = a.id
    on_plan(aid, "pro")
    yield aid
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def test_search_filters_dedupes_and_counts(acct, monkeypatch):
    fake = [
        place("o1", "Bright Smiles Dental", emails=("hi@brightsmiles.com",), phones=("+18655550101",),
              websites=("https://brightsmiles.com",)),
        place("o2", "Old Google Dental", phones=("865-555-0199",), websites=()),       # already a lead (from Google)
        place("o3", "Closed Dental", status="permanently_closed", phones=("+18655550103",)),
        place("o4", "Ghost Dental", confidence=0.1, phones=("+18655550104",)),           # unreliable
        place("o5", "No Contact Dental", phones=(), websites=()),                         # can't be reached
        place("o6", "Corner Pizza", category="pizza_restaurant", basic="restaurant", phones=("+18655550106",)),
        place("o7", "Chain Dental", brand="Aspen Dental", phones=("+18655550107",),
              websites=("https://facebook.com/chain", "https://chaindental.com")),
    ]
    monkeypatch.setattr(overture, "fetch_places", lambda z: fake)
    with Session(engine) as db:
        s = overture.search_and_save(db, acct, "37934", "Dentist")
        assert (s["found"], s["added"], s["skipped_closed"], s["skipped_unreliable"], s["skipped_duplicate"]) == (6, 2, 1, 2, 1)
        leads = {l.business_name: l for l in db.scalars(select(Lead).where(Lead.account_id == acct))}
        bright = leads["Bright Smiles Dental"]
        assert (bright.overture_id, bright.email, bright.status, bright.zip_code) == ("o1", "hi@brightsmiles.com", "New", "37934")
        assert leads["Chain Dental"].brand == "Aspen Dental" and leads["Chain Dental"].website == "https://chaindental.com"
        assert leads["Old Google Dental"].overture_id == "o2"  # older lead now backed by open data
        assert "Corner Pizza" not in leads
        assert db.scalar(select(ApiUsage.service).where(ApiUsage.account_id == acct)) == "overture_search"
        again = overture.search_and_save(db, acct, "37934", "Dentist")
        assert again["added"] == 0 and again["skipped_duplicate"] == 3  # never added twice


def test_limit_per_search_keeps_the_best(acct, monkeypatch):
    fake = [place(f"p{i}", f"Dental Office {i}", confidence=0.5 + i / 100, phones=(f"+1865555{1000 + i}",),
                  websites=(f"https://dental{i}.com",)) for i in range(40)]
    monkeypatch.setattr(overture, "fetch_places", lambda z: fake)
    with Session(engine) as db:
        s = overture.search_and_save(db, acct, "37934", "Dentist")
        assert (s["added"], s["skipped_over_limit"]) == (overture.MAX_ADDED, 40 - overture.MAX_ADDED)
        names = set(db.scalars(select(Lead.business_name).where(Lead.overture_id.is_not(None), Lead.account_id == acct)))
        assert "Dental Office 39" in names and "Dental Office 0" not in names  # most confident first


def test_search_stops_when_searches_are_used_up(acct, monkeypatch):
    monkeypatch.setattr(overture, "fetch_places", lambda z: [])
    with Session(engine) as db:
        for i in range(640):
            db.add(ApiUsage(account_id=acct, service="overture_search", detail=f"x{i}", est_cost_usd=0))
        db.commit()
        with pytest.raises(places.BudgetExceeded, match="used up"):
            overture.search_and_save(db, acct, "37934", "Dentist")


def test_type_matching_uses_names_only_when_category_is_vague():
    cats, words = overture_types.matcher("Tree service")
    pat = overture_types.name_pattern(words)
    assert overture.matches(place("a", "Big Tree Experts", category="contractor", basic="home_service"), cats, pat)
    assert not overture.matches(place("b", "Pear Tree Dental"), cats, pat)  # a dentist, despite the name
    cats, words = overture_types.matcher("CrossFit gym")  # no Overture category: name decides
    assert overture.matches(place("c", "CrossFit Farragut", category="gym", basic="gym"), cats,
                            overture_types.name_pattern(words))
    cats, words = overture_types.matcher("Axe throwing")  # typed by a customer
    assert overture.matches(place("d", "Knox Axe Throwing", category=None, basic=None), cats,
                            overture_types.name_pattern(words))


def test_zip_box_covers_the_zip():
    west, east, south, north = overture.zip_box("37934")
    assert west < -84.18 < east and south < 35.876 < north
    assert overture.zip_box("00000") is None


def test_independents_fill_the_limit_before_chains(acct, monkeypatch):
    chains = [place(f"c{i}", f"Chain Dental {i}", brand="Big Chain", confidence=0.99, phones=(f"+1865555{2000 + i}",),
                    websites=(f"https://chain{i}.com",)) for i in range(10)]
    locals_ = [place(f"l{i}", f"Local Dental {i}", confidence=0.6, phones=(f"+1865555{3000 + i}",),
                     websites=(f"https://local{i}.com",)) for i in range(overture.MAX_ADDED)]
    monkeypatch.setattr(overture, "fetch_places", lambda z: chains + locals_)
    with Session(engine) as db:
        overture.search_and_save(db, acct, "37934", "Dentist")
        brands = db.scalars(select(Lead.brand).where(Lead.account_id == acct, Lead.overture_id.is_not(None))).all()
        assert len(brands) == overture.MAX_ADDED and not any(brands)
