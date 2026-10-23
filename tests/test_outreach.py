"""Checks drafts, the Monday digest and renewal reminders with fake Gmail/Claude (no cost, nothing sent)."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tests.conftest import login_as, on_plan
from app import digest, gmail, outreach
from app.db import engine
from app.main import app
from app.models import Account, ApiUsage, FollowUp, Lead


@pytest.fixture
def ids():
    with Session(engine) as db:
        a = Account(name="Test: Outreach", gmail_token_enc="x", gmail_address="me@n2co.com",
                    business_description="We sell ads.", target_zips="37934")
        db.add(a)
        db.flush()
        leads = [Lead(account_id=a.id, business_name=f"Biz {i}", score=10 - i, zip_code="37934") for i in range(12)]
        leads.append(Lead(account_id=a.id, business_name="Already Won", score=10, status="Sold"))
        leads.append(Lead(account_id=a.id, business_name="Emailed Before", score=10, emailed_domain="old.com",
                          last_emailed_at=datetime.now(timezone.utc) - timedelta(days=20)))
        db.add_all(leads)
        db.commit()
        out = {"a": a.id, "first": leads[0].id, "emailed": leads[-1].id}
    on_plan(out["a"], "pro")
    yield out
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


@pytest.fixture
def fakes(monkeypatch):
    sent = {"drafts": [], "mail": []}
    monkeypatch.setattr(gmail, "can_compose", lambda a: True)
    monkeypatch.setattr(gmail, "gmail_service", lambda db, a: "svc")
    monkeypatch.setattr(gmail, "last_address", lambda svc, d: f"owner@{d}")
    monkeypatch.setattr(outreach, "write_email",
                        lambda a, l: (outreach.DraftEmail(subject=f"Hi {l.business_name}", body="Thanks,"), 0.001))
    monkeypatch.setattr(outreach, "save_draft", lambda svc, to, s, b: sent["drafts"].append((to, s)) or "d1")
    monkeypatch.setattr(digest, "send_to_self", lambda svc, to, s, b: sent["mail"].append((to, s, b)))
    return sent


def test_draft_top_skips_won_emailed_and_repeats(ids, fakes):
    with Session(engine) as db:
        a = db.get(Account, ids["a"])
        assert outreach.draft_top(db, a) == {"drafted": 10, "failed": 0}
        names = [s for _, s in fakes["drafts"]]
        assert names[0] == "Hi Biz 0" and "Hi Already Won" not in names and "Hi Emailed Before" not in names
        assert outreach.draft_top(db, a)["drafted"] == 2  # only the ones without a draft yet
        assert db.scalars(select(ApiUsage).where(ApiUsage.account_id == ids["a"],
                                                 ApiUsage.service == "claude_drafting")).all()


def test_single_draft_uses_known_address(ids, fakes):
    c = TestClient(app)
    login_as(c, ids["a"])
    r = c.post(f"/leads/{ids['emailed']}/draft")
    assert "Saved in your Gmail Drafts" in r.text
    assert fakes["drafts"] == [("owner@old.com", "Hi Emailed Before")]


def test_digest_content_and_schedule(ids, fakes):
    with Session(engine) as db:
        monday_9 = datetime(2026, 10, 19, 9, 0).astimezone()  # a Monday
        assert ids["a"] in digest.due_accounts(db, monday_9)
        subject = digest.send(db, ids["a"], monday_9)
        to, _, body = fakes["mail"][0]
        assert to == "me@n2co.com" and "Emailed Before" in body and "GONE QUIET" in body
        assert ids["a"] not in digest.due_accounts(db, monday_9 + timedelta(days=2))  # once a week
        assert ids["a"] in digest.due_accounts(db, monday_9 + timedelta(days=7))
        assert subject.startswith("Your week:")


def test_renewal_reminder(ids):
    from app.main import add_months
    c = TestClient(app)
    login_as(c, ids["a"])
    c.post(f"/leads/{ids['first']}/status", data={"status": "Sold", "contract_months": "3"})
    c.post(f"/leads/{ids['first']}/status", data={"status": "Sold", "contract_months": "3"})  # no duplicate
    with Session(engine) as db:
        sold_day = db.get(Lead, ids["first"]).sold_at.astimezone().date()
        end = add_months(sold_day, 3)
        fus = db.scalars(select(FollowUp).where(FollowUp.lead_id == ids["first"])).all()
        assert len(fus) == 1 and fus[0].due_date == max(date.today(), end - timedelta(days=30)) and "Renewal" in fus[0].task
    c.post(f"/leads/{ids['first']}/status", data={"status": "Sold", "contract_months": "15"})
    with Session(engine) as db:
        fus = db.scalars(select(FollowUp).where(FollowUp.lead_id == ids["first"])).all()
        assert [f.due_date for f in fus] == [add_months(sold_day, 15) - timedelta(days=30)]
