"""Checks the Google search with fake Google answers (no real requests, no cost)."""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import places
from app.db import engine
from app.models import Account, ApiUsage, Lead


def fake_place(pid, zip_code="37934", reviews=20, status="OPERATIONAL"):
    return {"id": pid, "displayName": {"text": f"Biz {pid}"}, "formattedAddress": "1 Test St",
            "addressComponents": [{"types": ["postal_code"], "shortText": zip_code}],
            "businessStatus": status, "userRatingCount": reviews, "rating": 4.5}


@pytest.fixture
def account_id():
    with Session(engine) as db:
        a = Account(name="Test: Places")
        db.add(a)
        db.commit()
        aid = a.id
    yield aid
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def test_filters_and_duplicates(account_id, monkeypatch):
    page = {"places": [fake_place("good"), fake_place("closed", status="CLOSED_PERMANENTLY"),
                       fake_place("few", reviews=1), fake_place("far", zip_code="90210")]}
    monkeypatch.setattr(places, "fetch_page", lambda q, t=None: page)
    with Session(engine) as db:
        s = places.search_and_save(db, account_id, "37934", "Dentist")
        assert (s["added"], s["skipped_closed"], s["skipped_few_reviews"], s["skipped_other_zip"]) == (1, 1, 1, 1)
        s2 = places.search_and_save(db, account_id, "37934", "Dentist")
        assert (s2["added"], s2["skipped_duplicate"]) == (0, 1)
        lead = db.scalars(select(Lead).where(Lead.account_id == account_id)).one()
        assert lead.google_place_id == "good" and lead.status == "New"
        assert len(db.scalars(select(ApiUsage).where(ApiUsage.account_id == account_id)).all()) == 2


def test_budget_stops_search(account_id, monkeypatch):
    calls = []
    monkeypatch.setattr(places, "fetch_page", lambda q, t=None: calls.append(q) or {"places": []})
    monkeypatch.setattr(places, "MONTHLY_API_BUDGET_USD", 0)
    with Session(engine) as db, pytest.raises(places.BudgetExceeded):
        places.search_and_save(db, account_id, "37934", "Dentist")
    assert calls == []
