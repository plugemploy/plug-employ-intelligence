""""May be closed": leads whose listed website is gone, or whose website says the business closed.

Nothing is removed automatically. A flagged lead gets a grey "May be closed" tag with the reason, sorts below the
open businesses and never takes a spot in a Free search's top 20. "Still open" clears the flag for good; "Closed"
removes the lead. Only strong signs count: a website address that no longer exists, a homepage that's gone (404) or a
"domain for sale" page, each seen twice a few minutes or more apart, or the website itself saying it closed.
No website, a slow or blocked site, or an old copyright year never count.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Lead

log = logging.getLogger(__name__)

REASONS = {
    "dead_dns": "Its website address no longer exists",
    "dead_404": "Its website is gone (page not found)",
    "dead_parked": 'Its website is now a "domain for sale" page',
}
SAYS = "Website says: "  # start of the reason when Claude read it on the site
RECHECK_AFTER = timedelta(minutes=5)  # a gone website must still be gone this much later before the flag goes on
FLAGGED_RECHECK = timedelta(days=7)  # flagged for a gone website: look again weekly; the flag comes off if it's back
EVERY_LEAD_RECHECK = timedelta(days=30)  # every other lead's website gets a light look about monthly
CHECKS_PER_RUN = 150
PARALLEL = 8


def now() -> datetime:
    return datetime.now(timezone.utc)


def flag(lead: Lead, reason: str) -> None:
    if lead.open_confirmed_at or lead.status == "Sold":  # "Still open" is final; customers aren't flagged
        return
    lead.closed_reason = reason[:300]
    lead.closed_flagged_at = lead.closed_flagged_at or now()


def saw_site(lead: Lead, status: str) -> None:
    """After any look at the lead's website: start the double-check, flag on the second sighting, or clear a
    gone-website flag when the site is back. "error" and "blocked" say nothing either way."""
    lead.closed_checked_at = now()
    if status.startswith("dead"):
        if lead.dead_since is None:
            lead.dead_since = lead.closed_checked_at
        elif lead.closed_checked_at - lead.dead_since >= RECHECK_AFTER:
            flag(lead, REASONS.get(status, REASONS["dead_404"]))
    elif status == "ok":
        lead.dead_since = None
        if lead.closed_reason in REASONS.values():
            lead.closed_reason = ""


def saw_closing_words(lead: Lead, words: str) -> None:
    """From scoring: Claude's short quote when the website says the business closed ("" when it doesn't)."""
    words = " ".join((words or "").split())
    if words:
        flag(lead, f'{SAYS}"{words[:200]}"')
    elif lead.closed_reason.startswith(SAYS):
        lead.closed_reason = ""  # re-scored and the site no longer says so


def due(db: Session, account_id: int, limit: int = CHECKS_PER_RUN, only_double_checks: bool = False) -> list[Lead]:
    """Leads to look at, most urgent first: gone websites waiting for their second look, then flagged ones due
    their weekly look, then any lead whose website hasn't had a look in a month."""
    t = now()
    base = (Lead.account_id == account_id, Lead.deleted_at.is_(None), Lead.website != "", Lead.website.is_not(None),
            Lead.open_confirmed_at.is_(None), Lead.status != "Sold")
    groups = [(Lead.dead_since.is_not(None), Lead.closed_reason == "", Lead.dead_since <= t - RECHECK_AFTER)]
    if not only_double_checks:
        groups += [(Lead.closed_reason.in_(REASONS.values()), Lead.closed_checked_at <= t - FLAGGED_RECHECK),
                   (Lead.website_checked_at.is_not(None), Lead.closed_reason == "", Lead.dead_since.is_(None),
                    or_(Lead.closed_checked_at.is_(None), Lead.closed_checked_at <= t - EVERY_LEAD_RECHECK))]
    leads, seen = [], set()
    for where in groups:
        for lead in db.scalars(select(Lead).where(*base, *where).order_by(Lead.score.desc().nulls_last(), Lead.id)
                               .limit(limit - len(leads))):
            if lead.id not in seen:
                seen.add(lead.id)
                leads.append(lead)
        if len(leads) >= limit:
            break
    return leads


def _look(url: str) -> str:
    from app import website
    try:
        return website.site_status(url)
    except Exception:  # one odd website must never stop the rest
        return "error"


def check_account(db: Session, account_id: int, limit: int = CHECKS_PER_RUN, only_double_checks: bool = False) -> dict:
    """Look at the websites that are due (see due), several at once; each is saved as soon as it's read."""
    stats = {"checked": 0, "flagged": 0, "cleared": 0}
    leads = due(db, account_id, limit, only_double_checks)
    if not leads:
        return stats
    with ThreadPoolExecutor(min(PARALLEL, len(leads))) as pool:
        looking = {pool.submit(_look, lead.website): lead for lead in leads}  # only URLs go to the threads
        for future in as_completed(looking):
            lead, status = looking[future], future.result()
            was = lead.closed_reason
            saw_site(lead, status)
            if status == "ok" or status.startswith("dead"):
                lead.website_status = status
            db.commit()
            stats["checked"] += 1
            stats["flagged"] += bool(lead.closed_reason and not was)
            stats["cleared"] += bool(was and not lead.closed_reason)
    return stats


def spawn(fn, *args):
    """Run fn in the background. Kept separate so tests can run it right away instead."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def double_check_soon(account_id: int) -> None:
    """After a search: give gone websites their second look once RECHECK_AFTER has passed (Free searches have
    no nightly run to do it)."""
    def run():
        from app.db import engine
        time.sleep(RECHECK_AFTER.total_seconds() + 30)
        try:
            with Session(engine) as db:
                check_account(db, account_id, limit=1000, only_double_checks=True)
        except Exception:
            log.exception("May-be-closed double check failed")
    spawn(run)
