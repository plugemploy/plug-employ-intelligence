"""Checks Claude scoring with fake Claude answers (no real requests, no cost)."""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import places, scoring
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
                        lambda lead, system: seen.append(lead.business_name) or (scoring.LeadScore(score=9, reason="Great fit."), 0.0001))
    with Session(engine) as db:
        s = scoring.score_unscored(db, account_id)
        assert s["scored"] == 1 and seen == ["Unscored Biz"]
        lead = db.scalars(select(Lead).where(Lead.business_name == "Unscored Biz", Lead.account_id == account_id)).one()
        assert (lead.score, lead.score_reason) == (9, "Great fit.") and lead.scored_at
        assert db.scalars(select(ApiUsage).where(ApiUsage.account_id == account_id)).one().service == "claude_scoring"


def test_budget_stops_scoring(account_id, monkeypatch):
    monkeypatch.setattr(scoring, "ask_claude", lambda lead, system: pytest.fail("should not call Claude"))
    monkeypatch.setattr(places, "MONTHLY_API_BUDGET_USD", 0)
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
