""""May be closed": leads that are probably out of business (Overture's open data lags behind).

- Listed website is gone (address no longer exists, homepage 404, "domain for sale" page), seen twice a few
  minutes or more apart: removed.
- Website didn't load (timed out, refused, server error): flagged "May be closed" and looked at again a day later;
  removed if it still doesn't load, cleared if it does. SSL, robots.txt and 401/403 never count (the site exists).
- No website, and Overture is unsure the place exists (confidence < NO_SITE_MIN_CONFIDENCE): removed (and new
  searches skip such places, overture._consider).
- Website says the business closed (Claude, when scoring): flagged only; a person decides.
- Website taken over by spam (gambling words, website.looks_hijacked; or Claude says the site isn't the business's,
  scoring site_not_theirs): the lead stays, but its website link is removed (kept in bad_website and shown on the
  lead page as text), so it never reaches Close or a draft. Not "May be closed".

Removed = moved to Deleted leads with closed_reason shown, and only leads nobody has worked on (removable: status
New, not in Close, not an imported client, never marked Still open). Others are flagged instead. A flagged lead gets
a grey tag, sorts last, never takes a spot in a Free search's top 20 and isn't sent to Close. "Still open" (or
Restore) clears the flag for good; "Closed" removes the lead.
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
DOWN_ONCE = "Its website didn't load. We'll check again tomorrow"
DOWN_TWICE = "Its website didn't load on two checks a day apart"
NO_SITE = "No website, and the map data isn't sure it's still open"
SITE_REASONS = (*REASONS.values(), DOWN_ONCE, DOWN_TWICE)  # cleared when the website works again
HIJACKED = "it showed gambling ads"
NO_SITE_MIN_CONFIDENCE = 0.5  # a place with no website needs Overture at least this sure it's real
DOWN_RECHECK = timedelta(hours=20)  # a website that didn't load is looked at again the next day
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


def removable(lead: Lead) -> bool:
    """Only leads nobody has worked on, so a business someone is talking to never disappears."""
    return (lead.deleted_at is None and lead.status == "New" and not lead.close_lead_id and lead.source != "import"
            and lead.open_confirmed_at is None)


def flag_or_remove(lead: Lead, reason: str) -> None:
    flag(lead, reason)
    if lead.closed_reason and removable(lead):
        lead.deleted_at = now()


def saw_site(lead: Lead, status: str) -> None:
    """After any look at the lead's website: start the double-check, remove (or flag) on the second sighting, or
    clear a website flag when the site is back. "error" and "blocked" say nothing either way."""
    lead.closed_checked_at = now()
    if status == "dead_hijacked":
        drop_website(lead, HIJACKED)
    elif status.startswith("dead"):
        if lead.dead_since is None:
            lead.dead_since = lead.closed_checked_at
        elif lead.closed_checked_at - lead.dead_since >= RECHECK_AFTER:
            flag_or_remove(lead, REASONS.get(status, REASONS["dead_404"]))
    elif status == "down":
        if lead.down_since is None:
            lead.down_since = lead.closed_checked_at
            if not lead.closed_reason:
                flag(lead, DOWN_ONCE)
        elif lead.closed_checked_at - lead.down_since >= DOWN_RECHECK:
            if lead.closed_reason in ("", DOWN_ONCE):
                lead.closed_reason = ""
                flag_or_remove(lead, DOWN_TWICE)
    elif status == "ok":
        lead.dead_since = lead.down_since = None
        if lead.closed_reason in SITE_REASONS:
            lead.closed_reason = ""


def saw_closing_words(lead: Lead, words: str) -> None:
    """From scoring: Claude's short quote when the website says the business closed ("" when it doesn't)."""
    words = " ".join((words or "").split())
    if words:
        flag(lead, f'{SAYS}"{words[:200]}"')
    elif lead.closed_reason.startswith(SAYS):
        lead.closed_reason = ""  # re-scored and the site no longer says so


def drop_website(lead: Lead, why: str) -> None:
    """The website isn't the business's any more (taken over by spam): keep the lead, remove the link."""
    if not lead.website:
        return
    lead.bad_website, lead.bad_website_why = lead.website[:300], why[:150]
    lead.website, lead.website_text, lead.website_about_url, lead.website_status = "", "", "", "no_site"
    lead.dead_since = lead.down_since = None
    if lead.closed_reason in SITE_REASONS:
        lead.closed_reason = ""


def saw_wrong_site(lead: Lead, words: str) -> None:
    """From scoring: Claude's few words when the website isn't the business's own (gambling, adult, another
    business)."""
    words = " ".join((words or "").split())
    if words:
        drop_website(lead, f"it showed {words}" if not words.lower().startswith(("it ", "a ", "an ")) else words)


def due(db: Session, account_id: int, limit: int = CHECKS_PER_RUN, only_double_checks: bool = False) -> list[Lead]:
    """Leads to look at, most urgent first: gone websites waiting for their second look, then flagged ones due
    their weekly look, then any lead whose website hasn't had a look in a month."""
    t = now()
    base = (Lead.account_id == account_id, Lead.deleted_at.is_(None), Lead.website != "", Lead.website.is_not(None),
            Lead.open_confirmed_at.is_(None), Lead.status != "Sold")
    groups = [(Lead.dead_since.is_not(None), Lead.closed_reason.in_(("", DOWN_ONCE)), Lead.dead_since <= t - RECHECK_AFTER)]
    if not only_double_checks:
        groups += [(Lead.down_since.is_not(None), Lead.closed_reason.in_(("", DOWN_ONCE)), Lead.down_since <= t - DOWN_RECHECK),
                   (Lead.closed_reason.in_(SITE_REASONS), Lead.closed_checked_at <= t - FLAGGED_RECHECK),
                   (Lead.website_status == "error", Lead.closed_checked_at.is_(None), Lead.closed_reason == "",
                    Lead.dead_since.is_(None), Lead.down_since.is_(None)),  # failed before these checks existed
                   (Lead.website_checked_at.is_not(None), Lead.closed_reason == "", Lead.dead_since.is_(None),
                    Lead.down_since.is_(None),
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
    stats = {"checked": 0, "flagged": 0, "cleared": 0, "removed": 0}
    if not only_double_checks:
        stats["removed"] += remove_unsure_without_site(db, account_id)
    leads = due(db, account_id, limit, only_double_checks)
    if not leads:
        return stats
    with ThreadPoolExecutor(min(PARALLEL, len(leads))) as pool:
        looking = {pool.submit(_look, lead.website): lead for lead in leads}  # only URLs go to the threads
        for future in as_completed(looking):
            lead, status = looking[future], future.result()
            was = lead.closed_reason
            saw_site(lead, status)
            if lead.website and (status in ("ok", "down") or status.startswith("dead")):
                lead.website_status = status
            db.commit()
            stats["checked"] += 1
            stats["flagged"] += bool(lead.closed_reason and not was)
            stats["cleared"] += bool(was and not lead.closed_reason)
            stats["removed"] += lead.deleted_at is not None
    return stats


def remove_unsure_without_site(db: Session, account_id: int) -> int:
    """Leads with no website that Overture isn't sure still exist."""
    n = 0
    for lead in db.scalars(select(Lead).where(
            Lead.account_id == account_id, Lead.deleted_at.is_(None), or_(Lead.website == "", Lead.website.is_(None)),
            Lead.source_confidence < NO_SITE_MIN_CONFIDENCE, Lead.closed_reason == "",
            Lead.bad_website == "")):  # its website was taken over: it had one, so it's kept
        if removable(lead):
            flag_or_remove(lead, NO_SITE)
            n += 1
    db.commit()
    return n


_last_daily: dict[int, float] = {}  # account -> when check_daily last ran it (resets when the app restarts)


def check_daily(account_ids: list[int]) -> None:
    """Every account with leads, about once a day, whether or not its nightly search is on."""
    from app.db import engine
    for account_id in account_ids:
        if time.time() - _last_daily.get(account_id, 0) < DOWN_RECHECK.total_seconds():
            continue
        _last_daily[account_id] = time.time()
        try:
            with Session(engine) as db:
                check_account(db, account_id)
        except Exception:
            log.exception("May-be-closed check failed")


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
