"""Checks importing current clients from a spreadsheet."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import client_import, results, search_jobs, scoring
from app.db import engine
from app.main import app
from app.models import Account, FollowUp, Lead, Organization
from tests.conftest import login_as, on_plan

CSV = (b"\xef\xbb\xbfCustomer Name,Phone Number,Website,Zip Code,Category,Monthly Rate,Term,Setup Fee,Date Signed\n"
       b"Bright Smiles Dental,(865) 555-0101,brightsmiles.com,37934,Dentist,$450,12 months,$250,10/01/2026\n"
       b"Glow Med Spa,865-555-0102,,37919,Med spa,\"$1,200\",1 year,,2026-09-15\n"
       b",,,,,,,,\n"
       b"No Details Bakery,,,,,,,,\n")


@pytest.fixture
def acct():
    with Session(engine) as db:
        a = Account(name="Test: Import")
        db.add(a)
        db.flush()
        db.add(Lead(account_id=a.id, business_name="Bright Smiles Dental, LLC", zip_code="37934", phone="8655550101"))
        db.commit()
        aid = a.id
    on_plan(aid, "pro")
    yield aid
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def test_parse_understands_common_headings():
    rows, found, problems = client_import.parse(CSV)
    assert [r["name"] for r in rows] == ["Bright Smiles Dental", "Glow Med Spa", "No Details Bakery"]
    first, second = rows[0], rows[1]
    assert (first["mrr"], first["contract_months"], first["ad_mgmt_fee"], first["start_date"]) == (450, 12, 250, date(2026, 10, 1))
    assert (second["mrr"], second["contract_months"], second["zip_code"]) == (1200, 12, "37919")
    assert found["name"] == "Customer Name" and found["mrr"] == "Monthly Rate" and not problems
    assert client_import.parse(b"Phone,Zip\n123,37934\n")[2]  # no name column: explained


def test_apply_marks_existing_and_adds_new_and_skips_results(acct):
    rows, _, _ = client_import.parse(CSV)
    with Session(engine) as db:
        assert client_import.preview(db, acct, rows) == {"total": 3, "matched": 1, "new": 2}
        assert client_import.apply(db, acct, rows) == {"marked_sold": 1, "added": 2}
        leads = {l.business_name: l for l in db.scalars(select(Lead).where(Lead.account_id == acct))}
        existing = leads["Bright Smiles Dental, LLC"]
        assert (existing.status, existing.source, float(existing.mrr), existing.contract_months) == ("Sold", "import", 450, 12)
        assert existing.sold_at.date() == date(2026, 10, 1) and existing.contacted_at  # earlier steps filled
        assert leads["Glow Med Spa"].status == "Sold" and leads["No Details Bakery"].status == "Sold"
        assert any("Renewal" in f.task for f in db.scalars(select(FollowUp).where(FollowUp.lead_id == existing.id)))
        assert results.metrics(db, [acct])[acct]["sold"] == 0  # imported clients aren't app sales


def test_tester_imports_through_pages(acct):
    c = TestClient(app)
    login_as(c, acct)
    assert "Import your current clients" in c.get("/business").text
    r = c.post("/clients/import/preview", files={"file": ("clients.csv", CSV, "text/csv")})
    assert "<b>3</b> clients" in r.text and "Import 3 clients" in r.text
    import re
    payload = re.search(r'name="payload" value="([^"]+)"', r.text).group(1)
    r = c.post("/clients/import/apply", data={"payload": payload})
    assert "Imported 3 clients" in r.text


def test_owner_imports_for_a_tester_and_others_cannot(acct, monkeypatch):
    monkeypatch.setattr(search_jobs, "spawn", lambda fn, *a: None)  # don't actually re-score
    with Session(engine) as db:
        owner_org = Organization(name="Test: Import Owner", plan="active", plan_tier="owner")
        db.add(owner_org)
        db.flush()
        mine = Account(name="Test: Import Owner Acct", org_id=owner_org.id)
        other = Account(name="Test: Import Stranger")
        db.add_all([mine, other])
        db.commit()
        mine_id, other_id = mine.id, other.id
    owner = TestClient(app)
    login_as(owner, mine_id)
    assert "Import current clients for Test: Import" in owner.get(f"/clients/import?account_id={acct}").text
    import base64
    r = owner.post("/clients/import/apply", data={"payload": base64.b64encode(CSV).decode(), "account_id": str(acct)},
                   follow_redirects=False)
    assert r.headers["location"].startswith("/beta?msg=Imported%203%20clients")
    scoring._rescoring.discard(acct)
    stranger = TestClient(app)
    login_as(stranger, other_id)
    assert stranger.get(f"/clients/import?account_id={acct}").status_code == 404
