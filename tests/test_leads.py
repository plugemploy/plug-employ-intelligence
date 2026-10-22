"""Checks the lead tracker, especially that accounts can't see each other's data."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from tests.conftest import login_as
from app.db import engine
from app.main import app
from app.models import Account, FollowUp, Lead


@pytest.fixture
def two_accounts():
    with Session(engine) as db:
        a, b = Account(name="Test: A"), Account(name="Test: B")
        db.add_all([a, b])
        db.flush()
        la = Lead(account_id=a.id, business_name="Alpha Test Lead", zip_code="37934")
        lb = Lead(account_id=b.id, business_name="Bravo Test Lead", zip_code="37922")
        db.add_all([la, lb])
        db.commit()
        ids = dict(a=a.id, b=b.id, la=la.id, lb=lb.id)
    yield ids
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def client_for(account_id):
    c = TestClient(app)
    login_as(c, account_id)
    return c


def test_list_shows_only_own_leads(two_accounts):
    html = client_for(two_accounts["a"]).get("/leads").text
    assert "Alpha Test Lead" in html
    assert "Bravo Test Lead" not in html


def test_cannot_open_or_change_other_accounts_lead(two_accounts):
    c = client_for(two_accounts["a"])
    lb = two_accounts["lb"]
    assert c.get(f"/leads/{lb}").status_code == 404
    assert c.post(f"/leads/{lb}/status", data={"status": "Won"}).status_code == 404
    assert c.post(f"/leads/{lb}/notes", data={"text": "hi"}).status_code == 404


def test_status_note_and_follow_up(two_accounts):
    c = client_for(two_accounts["a"])
    la = two_accounts["la"]
    assert c.post(f"/leads/{la}/status", data={"status": "Interested"}).status_code == 200
    assert "Spoke with owner" in c.post(f"/leads/{la}/notes", data={"text": "Spoke with owner"}).text
    assert c.post(f"/leads/{la}/follow-ups", data={"due_date": "2026-01-01", "task": "Call"}).status_code == 200
    assert "Alpha Test Lead" in c.get("/due").text
    with Session(engine) as db:
        fu = db.query(FollowUp).filter_by(lead_id=la).one()
        assert fu.account_id == two_accounts["a"]
        assert db.get(Lead, la).status == "Interested"
    # Account B can't mark A's follow-up done
    assert client_for(two_accounts["b"]).post(f"/follow-ups/{fu.id}/done").status_code == 404


def test_add_lead(two_accounts):
    r = client_for(two_accounts["a"]).post("/leads/new", data={"business_name": "New Test Biz"})
    assert r.status_code == 200 and "New Test Biz" in r.text


def test_industry_filter_and_sort(two_accounts):
    with Session(engine) as db:
        db.add(Lead(account_id=two_accounts["a"], business_name="Zed Florist", industry="Florist", score=5))
        db.add(Lead(account_id=two_accounts["a"], business_name="Amy Dental", industry="Dentist", score=9))
        db.commit()
    c = TestClient(app)
    login_as(c, two_accounts["a"])
    html = c.get("/leads?industry=Florist").text
    assert "Zed Florist" in html and "Amy Dental" not in html and 'selected>Florist' in html
    html = c.get("/leads?sort=industry").text
    assert html.index("Amy Dental") < html.index("Zed Florist")


def test_filters_remembered_after_opening_a_lead(two_accounts):
    c = TestClient(app)
    login_as(c, two_accounts["a"])
    c.get("/leads?zip_code=37934&sort=score")
    r = c.get("/leads", follow_redirects=False)  # e.g. "Back to leads" from a lead page
    assert r.status_code == 303 and "zip_code=37934" in r.headers["location"] and "sort=score" in r.headers["location"]
    c.get("/leads?clear=1")
    assert c.get("/leads", follow_redirects=False).status_code == 200


def test_search_bar(two_accounts):
    with Session(engine) as db:
        db.add(Lead(account_id=two_accounts["a"], business_name="Glow Spa", contact_name="Sarah Lee", address="1 Kingston Pike"))
        db.commit()
    c = TestClient(app)
    login_as(c, two_accounts["a"])
    for term in ("glow", "sarah lee", "kingston"):
        html = c.get(f"/leads?q={term}").text
        assert "Glow Spa" in html and "Alpha Test Lead" not in html
    assert "Bravo Test Lead" not in c.get("/leads?q=bravo").text  # other account never searchable
