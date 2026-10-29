"""Find leads in the background: the page returns right away and a bar on every page shows progress.

Each search is a SearchJob row, so the bar can show it on any page and after a reload.
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

import anthropic
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import gmail, overture, places, plans, scoring, signals
from app import website as site_check
from app.config import ANTHROPIC_API_KEY
from app.db import engine
from app.models import Account, SearchJob

log = logging.getLogger("search")
STALE_AFTER = timedelta(minutes=30)  # a "running" job this old was cut off by an app restart
SHOW_DONE_FOR = timedelta(hours=1)  # a finished job's bar shows this long unless dismissed


def spawn(fn, *args):
    """Run fn in the background. Kept separate so tests can run it right away instead."""
    threading.Thread(target=fn, args=args, daemon=True).start()


FREE_REASON_CHECKS = 150  # a Free search checks every lead's website for reasons to reach out, locked ones too


def reasons_after_free_search(db: Session, account_id: int) -> None:
    """A Free search company sees reasons to reach out on its first visit, not after a nightly run."""
    account = db.get(Account, account_id)
    if account is None or plans.plan_for(db, account) is not plans.PLANS["free"]:
        return

    def check(acct_id: int):
        try:
            with Session(engine) as s:
                signals.check_account(s, acct_id, limit=FREE_REASON_CHECKS)
        finally:
            signals.checking.pop(acct_id, None)
    signals.checking[account_id] = [0, 0]  # the leads page keeps refreshing from now, not only once the thread starts
    spawn(check, account_id)


def latest(db: Session, account_id: int) -> SearchJob | None:
    job = db.scalars(select(SearchJob).where(SearchJob.account_id == account_id)
                     .order_by(SearchJob.id.desc())).first()
    if job and job.status == "running" and job.started_at < datetime.now(timezone.utc) - STALE_AFTER:
        job.status, job.error = "error", "The search stopped part way (the app restarted). Please try again."
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
    return job


def for_bar(db: Session, account_id: int) -> SearchJob | None:
    """The job the bar should show: one still running, or one that finished recently and wasn't dismissed."""
    job = latest(db, account_id)
    if job is None or job.seen:
        return None
    if job.status == "running" or (job.finished_at and job.finished_at > datetime.now(timezone.utc) - SHOW_DONE_FOR):
        return job
    return None


def start(db: Session, account_id: int, zips: list[str], industries: list[str]) -> SearchJob:
    job = SearchJob(account_id=account_id, zips=json.dumps(zips), industries=json.dumps(industries))
    db.add(job)
    db.commit()
    spawn(run, job.id)
    return job


def run(job_id: int) -> None:
    """Search each ZIP x industry, then read websites, score new leads and check Gmail."""
    with Session(engine) as db:
        job = db.get(SearchJob, job_id)
        results = []
        try:
            recent = places.recently_searched(db, job.account_id)
            for z in job.zip_list:
                for ind in job.industry_list:
                    if f"{ind} in {z}" in recent:
                        results.append({"zip": z, "industry": ind, "recent": True})
                    else:
                        results.append(overture.search_and_save(db, job.account_id, z, ind))
                    job.results = json.dumps(results)
                    db.commit()
            job.step = "reading websites"
            db.commit()
            site_check.check_unchecked(db, job.account_id)
            if ANTHROPIC_API_KEY:
                job.step = "scoring"
                db.commit()
                scoring.score_unscored(db, job.account_id)
            if db.get(Account, job.account_id).gmail_token_enc:
                job.step = "checking Gmail"
                db.commit()
                try:
                    gmail.check_account(db, job.account_id)
                except Exception:  # a Gmail hiccup shouldn't fail the search
                    log.exception("Gmail check after search failed")
                    db.rollback()
            job.status = "done"
            reasons_after_free_search(db, job.account_id)
        except places.BudgetExceeded as exc:
            db.rollback()
            job.status, job.error = "error", str(exc) + " Search stopped."
        except anthropic.APIError as exc:
            db.rollback()
            job.status, job.error = "error", f"Claude returned an error: {str(exc).splitlines()[0]}"
        except httpx.HTTPError as exc:
            db.rollback()
            job.status, job.error = "error", f"Google returned an error: {str(exc).splitlines()[0]}"
        except Exception as exc:
            log.exception("Search job failed")
            db.rollback()
            job.status, job.error = "error", f"Something went wrong: {str(exc).splitlines()[0][:200]}"
        job.results = json.dumps(results)
        job.step = ""
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
