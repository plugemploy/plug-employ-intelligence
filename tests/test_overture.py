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


def test_short_name_words_also_match_longer_forms():
    # "roof" must stand alone, so "Roofing" needs its own word (a roofer listed only as a general contractor)
    for kind, name in [("Roofing", "Sample Roofing Co"), ("Brewery", "Sample Brewing Co"), ("Bike shop", "Sample Cycles"),
                       ("Bowling alley", "Sample Bowling Lanes"), ("Swim school", "Sample Swimming Academy")]:
        cats, words = overture_types.matcher(kind)
        assert overture.matches(place("r", name, category="contractor", basic="home_service"), cats,
                                overture_types.name_pattern(words)), kind
        assert overture.matches(place("n", name, category=None, basic=None), cats, overture_types.name_pattern(words)), kind


def test_thin_zip_fills_with_nearby(acct, monkeypatch):
    here = [place("h1", "Sample Roofing Here", category="roofing", basic="home_service", phones=("+18655550201",))]
    near = [{**place(f"n{i}", f"Sample Roofing {i}", category="roofing", basic="home_service",
                     phones=(f"+1865555{2300 + i}",), websites=(f"https://roof{i}.com",)),
             "postcode": "37922", "city": "FARRAGUT", "miles": 2.0 + i} for i in range(20)]
    near.append({**place("p1", "Sample Pizza", category="pizza_restaurant", basic="restaurant"),
                 "postcode": "37922", "city": "Farragut", "miles": 0.5})
    monkeypatch.setattr(overture, "fetch_places", lambda z: here)
    monkeypatch.setattr(overture, "fetch_nearby", lambda z: near)
    with Session(engine) as db:
        s = overture.search_and_save(db, acct, "37934", "Roofing")
        assert (s["added"], s["added_nearby"]) == (overture.FILL_TO, overture.FILL_TO - 1)
        leads = {l.business_name: l for l in db.scalars(select(Lead).where(Lead.account_id == acct, Lead.overture_id.is_not(None)))}
        assert leads["Sample Roofing Here"].nearby == ""
        assert (leads["Sample Roofing 0"].nearby, leads["Sample Roofing 0"].zip_code) == ("Farragut, 2 mi from 37934", "37922")
        assert "Sample Pizza" not in leads and "Sample Roofing 19" not in leads  # closest roofers first
        again = overture.search_and_save(db, acct, "37934", "Roofing")  # next month: the next closest ones
        assert again["added_nearby"] == 6 and "Sample Roofing 19" in {l.business_name for l in db.scalars(select(Lead))}


def test_full_zip_adds_no_nearby(acct, monkeypatch):
    here = [place(f"h{i}", f"Sample Dental {i}", phones=(f"+1865555{2400 + i}",), websites=(f"https://d{i}.com",))
            for i in range(overture.FILL_TO)]
    monkeypatch.setattr(overture, "fetch_places", lambda z: here)
    monkeypatch.setattr(overture, "fetch_nearby", lambda z: pytest.fail("no nearby needed"))
    with Session(engine) as db:
        assert overture.search_and_save(db, acct, "37934", "Dentist")["added_nearby"] == 0


def test_nearby_reach_by_zip_size():
    assert 9 < overture.nearby_miles("48178") <= overture.NEARBY_MAX_MILES  # South Lyon, MI: ~48 sq mi
    assert overture.nearby_miles("10001") == 3.0  # a small Manhattan ZIP: dense, so a short reach
    west, east, south, north = overture.zip_box("48178")
    assert (north - south) / 2 * 69 >= overture.nearby_miles("48178") - 0.01  # the read covers the nearby reach


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


def test_cached_zip_skips_the_line_and_accounts_run_side_by_side(monkeypatch):
    import threading
    import time
    from app import nightly
    monkeypatch.setattr(overture, "latest_release", lambda: "test-release")
    overture._zip_cache[("test-release", "37934")] = (time.time(), [{"id": "x", "postcode": "37934"},
                                                                    {"id": "y", "postcode": "37922"}])
    got = []
    with overture._lock:  # someone else's Overture read is running
        t = threading.Thread(target=lambda: got.append(overture.fetch_places("37934")))
        t.start()
        t.join(timeout=2)
    assert got == [[{"id": "x", "postcode": "37934"}]]  # answered from the cache without waiting, this ZIP only
    overture._zip_cache.clear()
    with nightly._account_lock(1):  # account 1 is mid-batch
        assert nightly._account_lock(2).acquire(timeout=1)  # account 2 isn't held up
        nightly._account_lock(2).release()
        assert not nightly._account_lock(1).acquire(blocking=False)  # account 1 can't run twice at once


def test_setup_counts_by_type(monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.conftest import login_as
    monkeypatch.setattr(overture, "latest_release", lambda: "test-release")
    monkeypatch.setattr(overture, "fetch_nearby", lambda z: [p for p in area if p["postcode"] != z])
    lat, lon, _ = overture._zips()["37934"]
    def at(pid, name, cat, postcode, **kw):
        return {**place(pid, name, category=cat, basic="home_service", **kw), "postcode": postcode, "city": "", "lat": lat, "lon": lon}
    area = [at("a", "Sample Roofing A", "roofing", "37934"), at("b", "Sample Roofing B", "roofing", "37934"),
            at("c", "Sample Roofing C", "roofing", "37922"), at("d", "Sample Ghost Roofing", "roofing", "37934", confidence=0.05),
            at("e", "Sample Dental", "dental_clinic", "37934")]
    overture._counts.clear()
    warmed = []
    monkeypatch.setattr(overture, "warm", lambda zips: warmed.append(zips))
    with Session(engine) as db:
        a = Account(name="Test: Counts")
        db.add(a)
        db.commit()
        aid = a.id
    c = TestClient(app)
    login_as(c, aid)
    assert "Counting businesses" in c.get("/setup/estimates?zips=37934").text and warmed == [["37934"]]
    overture._zip_cache[("test-release", "37934")] = (time.time(), area)
    try:
        counts = overture.estimates(["37934"], ["Roofing", "Dentist", "Florist"])
        assert counts == {"Roofing": (2, 1), "Dentist": (1, 0), "Florist": (0, 0)}  # the unreliable one isn't counted
        page = c.get("/setup/estimates?zips=37934").text
        assert '"Roofing": [2, 1]' in page and "applyIndCounts" in page
        assert 'hx-get="/setup/estimates"' in c.get("/setup").text
    finally:
        overture._zip_cache.clear()
        overture._counts.clear()
