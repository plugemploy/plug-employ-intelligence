"""Checks that Sold / Not interested results are saved and teach the scoring (no cost)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from tests.conftest import login_as
from app import scoring
from app.db import engine
from app.main import app
from app.models import Account, Lead


@pytest.fixture
def ids():
    with Session(engine) as db:
        a, b = Account(name="Test: Learn A"), Account(name="Test: Learn B")
        db.add_all([a, b])
        db.flush()
        spa = Lead(account_id=a.id, business_name="Glow Spa", industry="Med spa", zip_code="37934", rating=4.9, review_count=200)
        gym = Lead(account_id=a.id, business_name="Iron Gym", industry="Fitness", zip_code="37919")
        other = Lead(account_id=b.id, business_name="Secret Won", industry="Golf", status="Sold")
        db.add_all([spa, gym, other])
        db.commit()
        out = {"a": a.id, "spa": spa.id, "gym": gym.id}
    yield out
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def test_outcomes_saved_and_used(ids):
    c = TestClient(app)
    login_as(c, ids["a"])
    r = c.post(f"/leads/{ids['spa']}/status", data={"status": "Sold", "reason": "Loved the event idea"})
    assert "Why?" in r.text
    c.post(f"/leads/{ids['gym']}/status", data={"status": "Not interested", "reason": "No budget"})
    with Session(engine) as db:
        results = scoring.past_results(db, ids["a"])
        assert "Became customers:\n- Glow Spa | Med spa | ZIP 37934 | 4.9 from 200 reviews | reason: Loved the event idea" in results
        assert "Said no:\n- Iron Gym" in results and "No budget" in results
        assert "Secret Won" not in results  # another account's results never leak in
        system = scoring.build_system(db.get(Account, ids["a"]), results)
        assert "<past_results>" in system and "Glow Spa" in system
        w = scoring.whats_working(db, ids["a"])
        assert {"name": "Med spa", "won": 1, "lost": 0, "rate": 100} in w["industry"]
    assert "What&#39;s working" in c.get("/leads").text or "What's working" in c.get("/leads").text


def test_moving_back_clears_reason(ids):
    c = TestClient(app)
    login_as(c, ids["a"])
    c.post(f"/leads/{ids['spa']}/status", data={"status": "Sold", "reason": "x"})
    r = c.post(f"/leads/{ids['spa']}/status", data={"status": "Appointment set"})
    assert "Why?" not in r.text
    with Session(engine) as db:
        lead = db.get(Lead, ids["spa"])
        assert (lead.outcome_reason, lead.outcome_at) == ("", None)


def test_all_customers_counted_by_type(ids):
    with Session(engine) as db:
        for i in range(30):  # more customers than the examples shown
            db.add(Lead(account_id=ids["a"], business_name=f"Sample Dental {i}", industry="Dentist", status="Sold"))
        db.add(Lead(account_id=ids["a"], business_name="Sample Mystery Co", industry="", status="Sold"))
        db.commit()
        results = scoring.past_results(db, ids["a"])
    assert "All 31 customers by business type: Dentist x30, unknown x1" in results
    assert results.count("\n- ") == scoring.EXAMPLES_EACH
    assert "Secret Won" not in results


def test_no_results_means_no_section():
    assert "<past_results>" not in scoring.build_system(Account(business_description="x", target_zips=""), "")


def test_scoring_leans_on_customers_when_there_are_some():
    from types import SimpleNamespace
    from app import scoring
    acct = SimpleNamespace(zip_list=["37934"], business_description="We sell local ads.")
    with_customers = scoring.build_system(acct, "Became customers:\n- Glow Spa | Med spa | ZIP 37934 | rating unknown")
    assert "existing customers are the best guide" in with_customers and "8 or higher" in with_customers
    only_noes = scoring.build_system(acct, "Said no:\n- Big Box | Retail | ZIP 37934 | rating unknown")
    assert "existing customers are the best guide" not in only_noes and "said no" in only_noes
