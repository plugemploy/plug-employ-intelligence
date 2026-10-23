"""Checks Claude scoring with fake Claude answers (no real requests, no cost)."""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import places, plans, scoring
from app.db import engine
from app.models import Account, ApiUsage, Lead


@pytest.fixture
def account_id():
    with Session(engine) as db:
        a = Account(name="Test: Scoring")
        db.add(a)
        db.flush()
        db.add_all([Lead(account_id=a.id, business_name="Unscored Biz", zip_code="37934", rating=4.8, review_count=200),
                    Lead(account_id=a.id, business_name="Already Scored", score=3)])
        db.commit()
        aid = a.id
    yield aid
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def test_scores_only_unscored_and_logs_cost(account_id, monkeypatch):
    seen = []
    monkeypatch.setattr(scoring, "ask_claude",
                        lambda lead, system: seen.append(lead.business_name) or (scoring.LeadScore(score=9, reason="Great fit.", contact_name="Dr. Sarah Lee", contact_title="Owner"), 0.0001))
    with Session(engine) as db:
        s = scoring.score_unscored(db, account_id)
        assert s["scored"] == 1 and seen == ["Unscored Biz"]
        lead = db.scalars(select(Lead).where(Lead.business_name == "Unscored Biz", Lead.account_id == account_id)).one()
        assert (lead.score, lead.score_reason) == (9, "Great fit.") and lead.scored_at
        # no website text read, so a name can't have come from their website: not saved
        assert lead.contact_name == ""
        assert db.scalars(select(ApiUsage).where(ApiUsage.account_id == account_id)).one().service == "claude_scoring"


def test_budget_stops_scoring(account_id, monkeypatch):
    monkeypatch.setattr(scoring, "ask_claude", lambda lead, system: pytest.fail("should not call Claude"))
    monkeypatch.setattr(plans, "monthly_limit", lambda db, account: 0.0)
    with Session(engine) as db, pytest.raises(places.BudgetExceeded):
        scoring.score_unscored(db, account_id)


def test_describe_has_key_facts():
    text = scoring.describe(Lead(business_name="X", zip_code="37919", rating=4.5, review_count=12, website="https://x"))
    assert "37919" in text and "4.5" in text and "12 reviews" in text and "Website: yes" in text


def test_system_uses_account_description(account_id):
    with Session(engine) as db:
        a = db.get(Account, account_id)
        a.business_description, a.target_zips = "We sell billboards to car dealers.", "37919,37934"
        system = scoring.build_system(a)
        assert "billboards to car dealers" in system and "37919, 37934" in system
        a.business_description = ""
        assert scoring.DEFAULT_DESCRIPTION in scoring.build_system(a)


def test_contact_saved_from_website_and_used(account_id, monkeypatch):
    with Session(engine) as db:
        lead = db.scalars(select(Lead).where(Lead.account_id == account_id, Lead.business_name == "Unscored Biz")).one()
        lead.website_text = "Meet Dr. Sarah Lee, owner of Unscored Biz."
        db.commit()
    monkeypatch.setattr(scoring, "ask_claude", lambda lead, system: (
        scoring.LeadScore(score=8, reason="Fit.", contact_name=" Dr. Sarah Lee ", contact_title="Owner"), 0.0001))
    with Session(engine) as db:
        scoring.score_unscored(db, account_id)
        lead = db.scalars(select(Lead).where(Lead.account_id == account_id, Lead.business_name == "Unscored Biz")).one()
        assert (lead.contact_name, lead.contact_title) == ("Dr. Sarah Lee", "Owner")
        assert "Contact: Dr. Sarah Lee (Owner)" in scoring.describe(lead)
        from app import close_crm
        body = close_crm.lead_body(lead, "stat")
        assert body["contacts"][0]["name"] == "Dr. Sarah Lee" and body["contacts"][0]["title"] == "Owner"


def test_founded_year_from_website_only(account_id, monkeypatch):
    from datetime import datetime
    answers = iter([9999, 2021])
    monkeypatch.setattr(scoring, "ask_claude", lambda lead, system: (
        scoring.LeadScore(score=8, reason="Fit.", contact_name="", contact_title="", founded_year=next(answers)), 0.0001))
    with Session(engine) as db:
        lead = db.scalars(select(Lead).where(Lead.account_id == account_id, Lead.business_name == "Unscored Biz")).one()
        lead.website_text = "Proudly serving Farragut since 2021."
        db.commit()
        scoring.score_unscored(db, account_id)  # an impossible year is ignored
        assert db.get(Lead, lead.id).founded_year is None
        lead.score = None
        db.commit()
        scoring.score_unscored(db, account_id)
        lead = db.get(Lead, lead.id)
        assert lead.founded_year == 2021
        assert f"Founded: 2021 (about {datetime.now().year - 2021} years ago)" in scoring.describe(lead)


def test_rules_dont_penalize_multiple_locations():
    assert "several locations is not a weakness" in scoring.RUBRIC
    assert "affluent homeowners" in scoring.DEFAULT_DESCRIPTION


def test_rescore_all_button(account_id, monkeypatch):
    from fastapi.testclient import TestClient
    from app import search_jobs
    from app.main import app
    import app.main as m
    from tests.conftest import login_as
    monkeypatch.setattr(m, "ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(search_jobs, "spawn", lambda fn, *a: fn(*a))
    monkeypatch.setattr(scoring, "ask_claude", lambda lead, system: (
        scoring.LeadScore(score=6, reason="Rescored.", contact_name="", contact_title=""), 0.0001))
    with Session(engine) as db:
        for l in db.scalars(select(Lead).where(Lead.account_id == account_id)):
            l.score, l.score_reason = 9, "Old rules."
        db.commit()
    c = TestClient(app)
    login_as(c, account_id)
    r = c.post("/leads/rescore")
    assert "Re-scoring your" in r.text
    with Session(engine) as db:
        leads = db.scalars(select(Lead).where(Lead.account_id == account_id)).all()
        assert leads and all((l.score, l.score_reason) == (6, "Rescored.") for l in leads)
    assert account_id not in scoring._rescoring
