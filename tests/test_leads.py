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


def test_delete_hides_lead_and_restore_brings_it_back(two_accounts):
    c = client_for(two_accounts["a"])
    la = two_accounts["la"]
    c.post(f"/leads/{la}/delete")
    assert "Alpha Test Lead" not in c.get("/leads?sort=name").text
    assert "Alpha Test Lead" in c.get("/leads/deleted").text
    c.post(f"/leads/{la}/restore")
    assert "Alpha Test Lead" in c.get("/leads?sort=name").text
    assert "Alpha Test Lead" not in c.get("/leads/deleted").text


def test_cannot_delete_other_accounts_lead(two_accounts):
    c = client_for(two_accounts["a"])
    assert c.post(f"/leads/{two_accounts['lb']}/delete").status_code == 404
    assert c.post(f"/leads/{two_accounts['lb']}/restore").status_code == 404


def _count(account_id, name):
    from sqlalchemy import func, select
    with Session(engine) as db:
        return db.scalar(select(func.count()).select_from(Lead).where(
            Lead.account_id == account_id, Lead.business_name == name))


def test_add_lead_blocks_duplicates(two_accounts):
    c = client_for(two_accounts["a"])
    a = two_accounts["a"]
    with Session(engine) as db:
        db.add(Lead(account_id=a, business_name="Smoky Mtn Plumbing, LLC", zip_code="37934",
                    phone="(865) 555-0101", website="https://www.smokyplumb.com/about"))
        db.commit()
    # Same name (punctuation and LLC ignored) in the same ZIP
    html = c.post("/leads/new", data={"business_name": "smoky mtn plumbing", "zip_code": "37934"}).text
    assert "You already have this lead" in html
    # Same phone written differently
    assert "You already have this lead" in c.post("/leads/new", data={"business_name": "Other", "phone": "865.555.0101"}).text
    # Same website domain
    assert "You already have this lead" in c.post("/leads/new", data={"business_name": "Other", "website": "smokyplumb.com"}).text
    assert _count(a, "Other") == 0
    # Same name in another ZIP is a different location
    c.post("/leads/new", data={"business_name": "Smoky Mtn Plumbing", "zip_code": "37922"})
    assert _count(a, "Smoky Mtn Plumbing") == 1
    # Add anyway overrides
    c.post("/leads/new", data={"business_name": "Other", "phone": "8655550101", "add_anyway": "1"})
    assert _count(a, "Other") == 1


def test_duplicate_check_finds_deleted_lead_and_ignores_other_accounts(two_accounts):
    c = client_for(two_accounts["a"])
    c.post(f"/leads/{two_accounts['la']}/delete")
    html = c.post("/leads/new", data={"business_name": "Alpha Test Lead", "zip_code": "37934"}).text
    assert "in Deleted leads" in html and "Restore it" in html
    # Bravo belongs to account B, so A can add its own
    c.post("/leads/new", data={"business_name": "Bravo Test Lead", "zip_code": "37922"})
    assert _count(two_accounts["a"], "Bravo Test Lead") == 1


def test_shared_domains_are_not_duplicates(two_accounts):
    c = client_for(two_accounts["a"])
    with Session(engine) as db:
        db.add(Lead(account_id=two_accounts["a"], business_name="Page One", website="https://facebook.com/pageone"))
        db.commit()
    c.post("/leads/new", data={"business_name": "Page Two", "website": "https://www.facebook.com/pagetwo"})
    assert _count(two_accounts["a"], "Page Two") == 1


def test_search_leads_dates_and_newest(two_accounts):
    import json
    from datetime import datetime, timedelta, timezone
    from app.models import SearchJob
    from tests.conftest import on_plan
    a = two_accounts["a"]
    on_plan(a, "pro")
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        old = Lead(account_id=a, business_name="Old Lead", zip_code="37934", created_at=now - timedelta(days=10))
        new1 = Lead(account_id=a, business_name="From Search One", zip_code="37934", created_at=now - timedelta(minutes=5))
        new2 = Lead(account_id=a, business_name="From Search Two", zip_code="37934", created_at=now - timedelta(minutes=4))
        job = SearchJob(account_id=a, status="done", results=json.dumps([{"added": 2}]),
                        started_at=now - timedelta(minutes=6), finished_at=now - timedelta(minutes=3))
        other = SearchJob(account_id=two_accounts["b"], status="done")
        db.add_all([old, new1, new2, job, other])
        db.commit()
        job_id, other_id = job.id, other.id
    c = client_for(a)
    html = c.get(f"/leads?search={job_id}").text
    assert "From Search One" in html and "From Search Two" in html and "Old Lead" not in html
    assert "Showing the <b>2</b> leads added by your search" in html
    assert c.get(f"/leads?search={other_id}").status_code == 404  # another company's search
    day = (now - timedelta(days=10)).astimezone().date().isoformat()
    html = c.get(f"/leads?added_from={day}&added_to={day}").text
    assert "Old Lead" in html and "From Search One" not in html
    html = c.get("/leads?sort=newest").text
    assert html.index("From Search Two") < html.index("From Search One") < html.index("Old Lead")
    assert 'href="/leads?clear=1"' in html  # the logo clears filters


def test_only_the_owner_sees_dollar_amounts(two_accounts):
    from tests.conftest import on_plan
    a = two_accounts["a"]
    on_plan(a, "pro")
    c = client_for(a)
    for page in ("/search", "/usage", "/nightly"):
        html = c.get(page).text
        assert "$" not in html.split("<main", 1)[1], page
    assert "Searches by day" in c.get("/usage").text
    with Session(engine) as db:
        from app.models import Organization, Account
        db.get(Organization, db.get(Account, a).org_id).plan_tier = "owner"
        db.commit()
    assert "spent this month" in c.get("/search").text
