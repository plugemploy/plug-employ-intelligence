"""Checks the My business page and Gmail contacts matching (no network, no cost)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import contacts
from app.db import engine
from app.main import app
from app.models import Account, Contact, Lead

GOOGLE_CSV = (
    "First Name,Last Name,Organization Name,E-mail 1 - Label,E-mail 1 - Value\n"
    "Ann,Lee,,* Other,ann@farragutdental.com\n"
    "Bob,Ray,Knox Legacy Dental,,bob.ray@gmail.com ::: bob@work.com\n"
    "Cat,Ng,,,cat@gmail.com\n"
)


@pytest.fixture
def ids():
    with Session(engine) as db:
        a, b = Account(name="Test: Biz A"), Account(name="Test: Biz B")
        db.add_all([a, b])
        db.flush()
        db.add_all([
            Lead(account_id=a.id, business_name="Farragut Dental", website="https://www.farragutdental.com/"),
            Lead(account_id=a.id, business_name="Knox Legacy Dental, PLLC", website="https://knoxlegacy.com"),
            Lead(account_id=a.id, business_name="Gmail Only Salon", website="https://gmail.com"),
            Lead(account_id=b.id, business_name="Farragut Dental", website="https://farragutdental.com"),
        ])
        db.commit()
        out = {"a": a.id, "b": b.id}
    yield out
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def test_parse_and_match(ids):
    with Session(engine) as db:
        assert contacts.add_contacts(db, ids["a"], GOOGLE_CSV) == 4
        assert contacts.add_contacts(db, ids["a"], GOOGLE_CSV) == 0  # repeats skipped
        assert contacts.match_leads(db, ids["a"]) == 2
        marked = {l.business_name: l.contacted_match for l in db.scalars(select(Lead).where(Lead.account_id == ids["a"]))}
        assert marked == {"Farragut Dental": "ann@farragutdental.com", "Knox Legacy Dental, PLLC": "Knox Legacy Dental",
                          "Gmail Only Salon": ""}
        # other account untouched
        assert contacts.match_leads(db, ids["b"]) == 0


def test_bad_file_rejected():
    with pytest.raises(ValueError):
        contacts.parse_csv("Name,Phone\nA,555\n")


def test_business_page_save_and_upload(ids):
    c = TestClient(app)
    c.cookies.set("account_id", str(ids["a"]))
    r = c.post("/business", data={"description": "We sell billboards.", "zips": "37919, 37934\n12, 37919",
                                  "industries": ["Dentist", "Golf"], "extra_industries": "Florist, Dentist"})
    assert r.status_code == 200
    with Session(engine) as db:
        a = db.get(Account, ids["a"])
        assert (a.business_description, a.zip_list, a.industry_list) == (
            "We sell billboards.", ["37919", "37934"], ["Dentist", "Golf", "Florist"])
    r = c.post("/business/contacts", files={"file": ("contacts.csv", GOOGLE_CSV, "text/csv")})
    assert "2 of your leads" in r.text
    assert "Already contacted" in c.get("/leads").text
    assert "Farragut Dental" not in c.get("/leads?hide_contacted=1").text
    with Session(engine) as db:
        assert db.scalars(select(Contact).where(Contact.account_id == ids["b"])).first() is None
