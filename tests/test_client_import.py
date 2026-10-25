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


N2_EXPORT = (b"Clients\nMy Clients  (Managed: True)\n\n"
             b"Rep,Status,Office,Account,Name,Months in Print,Remaining Months in Print,Avg. Monthly Amount,DBA,"
             b"Billing Email,Sponsor Index Phone #,Website,Contact 1 Name,Contact 1 Phone,Contact 1 Address,Contact 1 Zip,"
             b"Contact 2 Name,Contact 2 Address,Contact 2 Zip\n"
             b"Rep A,Active,Office A,100001,Sample Insurance Agency,31,5,425.0,\"\",owner@sample-insurance.test,(555) 010-0001,,"
             b"Pat Sample,(555) 010-0001,\"1 Example Pike\",37000\n"
             b"Rep A,Active,Office A,100002,\"Lee & Kim Example, Realtors\",25,23,800.0,\"\",kim@example-realty.test,"
             b"(555) 010-0002,example-realty.test,Lee Example,,,,Kim Example,2 Sample Dr,37001\n"
             b"Rep A,Inactive,Office A,100003,Old Client,10,0,100.0,\"\",,,,,,,,,,\n")


def test_n2_style_export_with_title_lines(acct):
    rows, found, problems = client_import.parse(N2_EXPORT)
    assert [r["name"] for r in rows] == ["Sample Insurance Agency", "Lee & Kim Example, Realtors"]  # not "Account"
    assert problems == ["Skipped 1 rows whose status isn't active."]
    first, second = rows
    assert (first["mrr"], first["months_in_print"], first["remaining_months"], first["zip_code"]) == (425, 31, 5, "37000")
    assert (second["zip_code"], second["contact_name"]) == ("37001", "Lee Example")  # Contact 2 fills an empty Contact 1
    with Session(engine) as db:
        client_import.apply(db, acct, rows)
        lead = db.scalars(select(Lead).where(Lead.account_id == acct, Lead.business_name == "Sample Insurance Agency")).one()
        assert (lead.contract_months, lead.email, lead.contact_name) == (36, "owner@sample-insurance.test", "Pat Sample")
        months_ago = (date.today().year - lead.sold_at.year) * 12 + date.today().month - lead.sold_at.month
        assert months_ago == 31  # client since: 31 months in print
        renewal = db.scalars(select(FollowUp).where(FollowUp.lead_id == lead.id)).one()
        assert "Renewal" in renewal.task  # contract ends in 5 months


def test_bare_websites_become_full_addresses_and_lead_page_shows_details(acct):
    from app.website import full_url
    assert full_url(" example.test ") == "https://example.test" and full_url("http://a.test") == "http://a.test"
    assert full_url("") == "" and full_url("not a site") == "" and full_url("me@x.test") == ""
    rows, _, _ = client_import.parse(b"Name,Website,Email,Contact Name\nSample Spa,www.sample-spa.test,hi@sample-spa.test,Pat Owner\n")
    assert rows[0]["website"] == "https://www.sample-spa.test"
    with Session(engine) as db:
        client_import.apply(db, acct, rows)
        lead_id = db.scalars(select(Lead.id).where(Lead.account_id == acct, Lead.business_name == "Sample Spa")).one()
    c = TestClient(app)
    login_as(c, acct)
    page = c.get(f"/leads/{lead_id}").text
    assert 'href="https://www.sample-spa.test"' in page and "mailto:hi@sample-spa.test" in page
    assert "(from your client list)" in page and "imported client" in page and "(from their website)" not in page
