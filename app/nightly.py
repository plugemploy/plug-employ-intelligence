"""Overnight lead search: runs a batch of Google searches, then scores new leads with Claude.

The scheduler checks every 15 minutes. If an account's run time has passed today and it hasn't
run yet (for example, the Mac was asleep at 2 a.m.), it runs then.
"""

import logging
import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import close_crm, digest, gmail, places, scoring, website
from app.config import ANTHROPIC_API_KEY
from app.db import engine
from app.models import Account, ApiUsage, Lead, NightlyRun, NightlySettings

log = logging.getLogger("nightly")
_lock = threading.Lock()
RESEARCH_AFTER_DAYS = 30  # don't repeat a ZIP + industry search within this many days


def get_settings(db: Session, account_id: int) -> NightlySettings:
    s = db.scalars(select(NightlySettings).where(NightlySettings.account_id == account_id)).first()
    if s is None:
        s = NightlySettings(account_id=account_id, enabled=False, run_hour=2, searches_per_night=20)
        db.add(s)
        db.commit()
    return s


def next_pairs(db: Session, s: NightlySettings) -> list[tuple[str, str]]:
    """The next ZIP + industry searches, skipping ones done in the last 30 days."""
    since = datetime.now().astimezone() - timedelta(days=RESEARCH_AFTER_DAYS)
    done = set(db.scalars(select(ApiUsage.detail).where(
        ApiUsage.account_id == s.account_id, ApiUsage.service == "google_places_text_search",
        ApiUsage.created_at >= since)))
    account = db.get(Account, s.account_id)
    zips, inds = account.zip_list, account.industry_list  # from the "My business" page
    pairs = [(z, i) for z in zips for i in inds if f"{i} in {z}" not in done]
    return pairs[: s.searches_per_night]


def run_for_account(account_id: int, max_searches: int | None = None) -> NightlyRun:
    """Run one batch now. Only one batch runs at a time."""
    with _lock, Session(engine) as db:
        s = get_settings(db, account_id)
        run = NightlyRun(account_id=account_id)
        db.add(run)
        db.commit()
        try:
            for z, ind in next_pairs(db, s)[:max_searches]:
                stats = places.search_and_save(db, account_id, z, ind)
                run.searches += 1
                run.added += stats["added"]
            try:
                gmail.check_account(db, account_id)
            except Exception:  # a Gmail hiccup shouldn't stop the lead search
                log.exception("Gmail check failed")
            website.check_unchecked(db, account_id, limit=1000)
            if ANTHROPIC_API_KEY:
                scoring.score_unscored(db, account_id, limit=1000)
            if close_crm.enabled_for(db, db.get(Account, account_id)):
                try:
                    close_crm.sync_back(db, account_id)
                    if db.get(Account, account_id).close_auto_send:
                        close_crm.send_leads(db, close_crm.top_unsent(db, account_id, min_score=9, limit=50))
                except Exception:  # a Close hiccup shouldn't stop the lead search
                    log.exception("Close sync failed")
                    db.rollback()
        except Exception as exc:  # keep the error for the morning summary
            log.exception("Nightly run failed")
            run.error = str(exc).splitlines()[0][:500]
        run.high_scores = db.scalar(select(func.count()).select_from(Lead).where(
            Lead.account_id == account_id, Lead.created_at >= run.started_at, Lead.score >= 8)) or 0
        run.finished_at = func.now()
        db.commit()
        db.refresh(run)
        return run


def due_accounts(db: Session, now: datetime) -> list[int]:
    """Accounts whose run time has passed today and that haven't run since then."""
    due = []
    for s in db.scalars(select(NightlySettings).where(NightlySettings.enabled.is_(True))):
        scheduled = now.replace(hour=s.run_hour, minute=0, second=0, microsecond=0)
        if now < scheduled:
            continue
        last = db.scalar(select(func.max(NightlyRun.started_at)).where(NightlyRun.account_id == s.account_id))
        if last is None or last < scheduled:
            due.append(s.account_id)
    return due


NIGHTLY_LOCK_ID = 734221  # any fixed number; shared by every copy of the app


def check_and_run():
    """Run due accounts. A database lock stops two copies of the app from both running."""
    with engine.connect() as conn:
        if not conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": NIGHTLY_LOCK_ID}):
            return
        try:
            with Session(engine) as db:
                ids = due_accounts(db, datetime.now().astimezone())
            for account_id in ids:
                run_for_account(account_id)
            with Session(engine) as db:
                for account_id in digest.due_accounts(db, datetime.now().astimezone()):
                    try:
                        digest.send(db, account_id)
                    except Exception:
                        log.exception("Monday digest failed")
                        db.rollback()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": NIGHTLY_LOCK_ID})


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler()
    sched.add_job(check_and_run, "interval", minutes=15, next_run_time=datetime.now(), max_instances=1, coalesce=True)
    sched.start()
    return sched
