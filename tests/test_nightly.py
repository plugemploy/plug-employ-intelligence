"""Checks the nightly search with fake Google/Claude (no real requests, no cost)."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import nightly, places, scoring
from app.db import engine
from app.models import Account, ApiUsage, NightlyRun


@pytest.fixture
def account_id():
    with Session(engine) as db:
        a = Account(name="Test: Nightly")
        db.add(a)
        db.commit()
        aid = a.id
    yield aid
    with Session(engine) as db:
        db.execute(delete(Account).where(Account.name.like("Test:%")))
        db.commit()


def test_next_pairs_skips_recent_and_limits(account_id):
    with Session(engine) as db:
        st = nightly.get_settings(db, account_id)
        st.zips, st.industries, st.searches_per_night = "37934,37919", "Dentist,Salon", 3
        db.add(ApiUsage(account_id=account_id, service="google_places_text_search",
                        detail="Dentist in 37934", est_cost_usd=0.035))
        db.commit()
        assert nightly.next_pairs(db, st) == [("37934", "Salon"), ("37919", "Dentist"), ("37919", "Salon")]


def test_due_only_after_run_time_and_once(account_id):
    with Session(engine) as db:
        st = nightly.get_settings(db, account_id)
        st.enabled, st.run_hour = True, 2
        db.commit()
        now = datetime.now().astimezone().replace(hour=1, minute=0)
        assert account_id not in nightly.due_accounts(db, now)            # before 2 a.m.
        now = now.replace(hour=9)
        assert account_id in nightly.due_accounts(db, now)                # missed 2 a.m., catch up
        db.add(NightlyRun(account_id=account_id, started_at=now - timedelta(hours=1)))
        db.commit()
        assert account_id not in nightly.due_accounts(db, now)            # already ran today


def test_run_records_results(account_id, monkeypatch):
    calls = []
    monkeypatch.setattr(places, "search_and_save",
                        lambda db, aid, z, i: calls.append((z, i)) or {"added": 2})
    monkeypatch.setattr(scoring, "score_unscored", lambda db, aid, limit: {})
    with Session(engine) as db:
        st = nightly.get_settings(db, account_id)
        st.zips, st.industries, st.searches_per_night = "37934", "Dentist,Salon,Golf", 2
        db.commit()
    run = nightly.run_for_account(account_id)
    assert calls == [("37934", "Dentist"), ("37934", "Salon")]
    assert (run.searches, run.added, run.error) == (2, 4, "") and run.finished_at


def test_run_keeps_error(account_id, monkeypatch):
    def boom(*a):
        raise places.BudgetExceeded("Monthly API budget of $50 reached.")
    monkeypatch.setattr(places, "search_and_save", boom)
    run = nightly.run_for_account(account_id, max_searches=1)
    assert "budget" in run.error


def test_second_copy_skips_while_locked(monkeypatch):
    from sqlalchemy import text
    monkeypatch.setattr(nightly, "due_accounts", lambda db, now: pytest.fail("should not run"))
    with engine.connect() as other:  # pretend another copy of the app holds the lock
        other.scalar(text("SELECT pg_advisory_lock(:k)"), {"k": nightly.NIGHTLY_LOCK_ID})
        nightly.check_and_run()
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": nightly.NIGHTLY_LOCK_ID})
