"""New leads in one pass: read each website once, score it right away and (for a Free search) look for reasons
to reach out from the same visit. Several leads at once; each is saved as soon as it's done, so scores and 🔔
reasons show up on the leads page while the rest are still being read.

Threads only do network work (the website, Claude) on plain copies of the leads; saving happens here, one at a time.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

import anthropic
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import scoring, signals, website
from app.config import ANTHROPIC_API_KEY
from app.models import Account, Lead
from app.places import BudgetExceeded, check_budget

PARALLEL = 8


def _work(copy, system: str, reasons: bool, today: date) -> dict:
    """Read the site (if not read yet), score, and find reasons. Network only: copy is a plain snapshot."""
    out = {}
    if copy.website_checked_at is None:
        status, text, about, news_text, news_url = website.read_site(copy.website or "")
        out["site"] = (status, text, about)
        copy.website_status, copy.website_text, copy.website_about_url = status, text, about
        if status == "ok":
            copy.score = None  # score with what the website says
        if reasons and news_text:
            out["news"] = (news_text, news_url)
    if system and copy.score is None:
        try:
            out["score"] = scoring.ask_claude(copy, system)
        except (anthropic.APIError, ValueError) as exc:
            out["score"] = exc
    if "news" in out:
        try:
            found, cost = signals.ask_claude(copy, out["news"][0], today)
            out["reasons"] = (out["news"][0], out["news"][1], found, cost)
        except (anthropic.APIError, ValueError) as exc:
            out["reasons"] = exc
    return out


def run(db: Session, account_id: int, reasons: bool = False, limit: int = 1000) -> dict:
    """Read, score (and with reasons=True check for reasons to reach out) this account's new leads."""
    stats = {"read": 0, "scored": 0, "reasons": 0, "failed": 0}
    leads = db.scalars(select(Lead).where(
        Lead.account_id == account_id, Lead.deleted_at.is_(None),
        or_(Lead.website_checked_at.is_(None), Lead.score.is_(None))).order_by(Lead.id).limit(limit)).all()
    if not leads:
        return stats
    system = (scoring.build_system(db.get(Account, account_id), scoring.past_results(db, account_id))
              if ANTHROPIC_API_KEY else "")
    est = scoring.EST_COST_PER_LEAD + (signals.EST_COST if reasons else 0)
    today, queue = date.today(), iter(leads)
    with ThreadPoolExecutor(PARALLEL) as pool:
        working = {}

        def start_next() -> bool:
            lead = next(queue, None)
            if lead is None:
                return False
            if system:
                check_budget(db, account_id, est)
            working[pool.submit(_work, scoring.snapshot(lead), system, reasons and bool(system), today)] = lead
            return True
        try:
            for _ in range(PARALLEL):
                start_next()
        except BudgetExceeded:
            queue = iter(())
        while working:
            future = next(as_completed(working))
            lead = working.pop(future)
            try:
                out = future.result()
                if "site" in out:
                    lead.website_status, lead.website_text, lead.website_about_url = out["site"]
                    lead.website_checked_at = datetime.now(timezone.utc)
                    if out["site"][0] == "ok":
                        lead.score = None
                    db.commit()
                    stats["read"] += 1
                if isinstance(out.get("score"), tuple):
                    scoring.save(db, lead, *out["score"])
                    stats["scored"] += 1
                elif "score" in out:
                    stats["failed"] += 1
                if reasons and "site" in out and system:
                    found = out.get("reasons")
                    if isinstance(found, Exception):
                        stats["failed"] += 1
                    else:  # no news text: still marked checked, so it isn't read again for reasons
                        stats["reasons"] += signals.save(db, lead, found or ("", "", [], 0.0), today)
            except Exception:  # one odd website or answer must never stop the rest
                db.rollback()
                stats["failed"] += 1
            try:
                start_next()
            except BudgetExceeded:
                queue = iter(())
    return stats
