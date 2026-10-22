"""Checks the Close connection with a fake Close (no real requests)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tests.conftest import login_as
from app import close_crm
from app.db import engine
from app.main import app
from app.models import Account, FollowUp, Lead


class FakeClose:
    def __init__(self, existing=None):
        self.leads = existing or []
        self.created, self.notes = [], []

    def statuses(self):
        return {"potential": "stat_pot"}

    def all_leads(self):
        return self.leads

    def create_lead(self, body):
        cid = f"lead_{len(self.created)}"
        self.created.append(body)
        self.leads.append({"id": cid, "display_name": body["name"], "url": body.get("url"), "status_label": "Potential"})
        return cid

    def add_note(self, cid, note):
        self.notes.append((cid, note))


@pytest.fixture
def ids():
    with Session(engine) as db:
        a = Account(name="Test: Close")
        db.add(a)
        db.flush()
        db.add_all([
            Lead(account_id=a.id, business_name="Top Spa", phone="865-555-0101", website="https://topspa.com", score=9, score_reason="Great fit"),
            Lead(account_id=a.id, business_name="Known Dental", phone="865-555-0102", website="https://www.knowndental.com", score=8),
            Lead(account_id=a.id, business_name="No Phone", phone="", score=10),
            Lead(account_id=a.id, business_name="Won Already", phone="1", score=10, status="Won"),
        ])
        db.commit()
        yield a.id
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def test_send_skips_duplicates_and_sync_back(ids):
    fake = FakeClose(existing=[{"id": "lead_x", "display_name": "Known Dental LLC", "url": "http://knowndental.com/",
                                "status_label": "Qualified"}])
    with Session(engine) as db:
        leads = close_crm.top_unsent(db, ids)
        assert [l.business_name for l in leads] == ["Top Spa", "Known Dental"]
        s = close_crm.send_leads(db, leads, fake)
        assert s == {"sent": 1, "already_in_close": 1, "failed": 0}
        body = fake.created[0]
        assert body["status_id"] == "stat_pot" and body["contacts"][0]["phones"][0]["phone"] == "865-555-0101"
        assert "Great fit" in fake.notes[0][1]
        assert close_crm.top_unsent(db, ids) == []  # never sent twice

        # VA works the leads in Close
        fake.leads[1]["status_label"] = "Bad Fit"      # Top Spa
        fake.leads[0]["status_label"] = "3-6 Months"   # Known Dental
        assert close_crm.sync_back(db, ids, fake) == {"updated": 2}
        spa = db.scalars(select(Lead).where(Lead.account_id == ids, Lead.business_name == "Top Spa")).one()
        dental = db.scalars(select(Lead).where(Lead.account_id == ids, Lead.business_name == "Known Dental")).one()
        assert (spa.status, spa.outcome_reason) == ("Not interested", "Bad fit")
        assert dental.status == "Contacted"
        fu = db.scalars(select(FollowUp).where(FollowUp.lead_id == dental.id)).one()
        assert (fu.due_date - date.today()).days == 90
        assert close_crm.sync_back(db, ids, fake) == {"updated": 0}  # nothing new, no repeat follow-ups


def test_unknown_status_changes_nothing(ids):
    with Session(engine) as db:
        lead = db.scalars(select(Lead).where(Lead.account_id == ids, Lead.business_name == "Top Spa")).one()
        assert close_crm.apply_status(db, lead, "FACEBOOK GEN") and lead.status == "New"
        assert close_crm.apply_status(db, lead, "Customer") and lead.status == "Won"


def test_pages_show_close(ids, monkeypatch):
    monkeypatch.setattr(close_crm, "enabled_for", lambda db, a: True)
    c = TestClient(app)
    login_as(c, ids)
    assert "Send top 20 to Close" in c.get("/leads").text
    assert "send new leads scored 9+ to Close" in c.get("/nightly").text
