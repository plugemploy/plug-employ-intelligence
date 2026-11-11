"""A second opinion from Foursquare's open places data (FSQ OS Places, Apache 2.0, attribution in /privacy).

Read live through Foursquare's Places Portal (Iceberg catalog, FOURSQUARE_TOKEN), one ZIP code at a time: about
1-7 seconds and ~400 MB, kept for 12 hours. Used to:
- skip Overture places Foursquare marks closed (overture._consider),
- add Foursquare's phone number as Other phone when it differs (new leads, and the monthly check of old ones),
- remove or flag existing leads Foursquare marks closed (check_account, monthly, via app/closed.py).
Started in the background as soon as a new customer types their ZIP codes (with overture.warm).

If the token is missing or expired, every function returns nothing and searches carry on with Overture alone;
the owner sees a notice on /beta (token_status).
"""

import base64
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import closed, phones
from app.config import FOURSQUARE_TOKEN
from app.dedupe import _name_key
from app.models import Lead

log = logging.getLogger(__name__)

CATALOG = "https://catalog.h3-hub.foursquare.com/iceberg"
CACHE_SECONDS = 12 * 3600
CACHE_ZIPS = 30
RECHECK_DAYS = 30  # existing leads are compared again about monthly (Foursquare releases monthly)
REASON = "Foursquare lists it as closed"

_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()  # one Foursquare read at a time: caps memory
_warming: set[str] = set()
last_error = {"text": "", "at": 0.0}  # shown to the owner on /beta


def configured() -> bool:
    return bool(FOURSQUARE_TOKEN)


def token_expires() -> datetime | None:
    """When the Places Portal token stops working (it's a JWT with an exp date), or None if unknown."""
    try:
        payload = FOURSQUARE_TOKEN.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
        return datetime.fromtimestamp(exp, timezone.utc)
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def token_status() -> str:
    """A line for the owner when the token needs attention, else ""."""
    if not configured():
        return ""
    ends = token_expires()
    now = datetime.now(timezone.utc)
    if ends and ends <= now:
        return f"The Foursquare key expired on {ends.astimezone():%b %-d}. Searches are using Overture only until it's renewed."
    if ends and ends - now <= timedelta(days=5):
        return f"The Foursquare key expires on {ends.astimezone():%b %-d}. Renew it on the Places Portal and update FOURSQUARE_TOKEN in Render."
    if last_error["text"] and time.time() - last_error["at"] < 24 * 3600:
        return f"Foursquare couldn't be read recently ({last_error['text']}). Searches are using Overture only."
    return ""


def _connect():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL iceberg; LOAD iceberg; SET memory_limit='400MB'; SET threads=2;")
    con.execute("CREATE SECRET fsq (TYPE iceberg, TOKEN ?)", [FOURSQUARE_TOKEN])
    con.execute(f"ATTACH 'places' AS fsq (TYPE iceberg, SECRET fsq, ENDPOINT '{CATALOG}')")
    return con


def _read(zip_code: str) -> list[dict]:
    from app import overture
    west, east, south, north = overture.zip_box(zip_code)
    con = _connect()
    try:
        rows = con.execute("""
            SELECT name, tel, website, date_refreshed, date_closed FROM fsq.datasets.places_os
            WHERE country = 'US' AND longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ? AND postcode = ?""",
            [west, east, south, north, zip_code]).fetchall()
    finally:
        con.close()
    return [dict(name=r[0] or "", phone=phones.key(r[1] or ""), website=r[2] or "", closed=r[4] is not None) for r in rows]


def fetch_zip(zip_code: str) -> list[dict] | None:
    """Foursquare's places in this ZIP code (kept 12 hours), or None when it can't be read. Kept separate so tests
    can replace it."""
    from app import overture
    if not configured() or overture.zip_box(zip_code) is None:
        return None
    hit = _cache.get(zip_code)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    ends = token_expires()
    if ends and ends <= datetime.now(timezone.utc):
        return None
    with _lock:
        hit = _cache.get(zip_code)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        try:
            places = _read(zip_code)
        except Exception as exc:  # expired token, network: carry on with Overture alone
            log.warning("Foursquare read failed for %s: %s", zip_code, exc)
            last_error.update(text=str(exc).splitlines()[0][:120], at=time.time())
            return None
        last_error.update(text="", at=0.0)
        if len(_cache) >= CACHE_ZIPS:
            _cache.clear()
        _cache[zip_code] = (time.time(), places)
        return places


def warm(zips: list[str]) -> None:
    """Read these ZIPs in the background (one at a time), so the first search has Foursquare ready."""
    if not configured():
        return
    todo = [z for z in zips if z not in _warming and z not in _cache]
    if not todo:
        return
    _warming.update(todo)

    def run():
        for z in todo:
            try:
                fetch_zip(z)
            finally:
                _warming.discard(z)
    threading.Thread(target=run, daemon=True).start()


class Index:
    """Foursquare's places in a ZIP, looked up by phone number or business name."""

    def __init__(self, places: list[dict]):
        self.by_phone = {p["phone"]: p for p in places if p["phone"]}
        self.by_name = {}
        for p in places:
            key = _name_key(p["name"])
            if key:
                # an open listing wins over a closed one with the same name (a business that moved within the ZIP)
                if key not in self.by_name or self.by_name[key]["closed"]:
                    self.by_name[key] = p

    def find(self, name: str, phone: str) -> dict | None:
        return self.by_phone.get(phones.key(phone)) if phones.key(phone) in self.by_phone else self.by_name.get(_name_key(name))


def index_for(zip_code: str) -> Index | None:
    places = fetch_zip(zip_code)
    return Index(places) if places is not None else None


def other_phone(lead: Lead, match: dict | None) -> None:
    """Foursquare's number as Other phone when it differs from the lead's and there isn't one yet."""
    if match and match["phone"] and not lead.other_phone and match["phone"] != phones.key(lead.phone):
        if not lead.phone:
            lead.phone, lead.phone_source = phones.stored(match["phone"]), "foursquare"
        else:
            lead.other_phone, lead.other_phone_source = phones.stored(match["phone"]), "foursquare"


def check_account(db: Session, account_id: int, limit: int = 2000) -> dict:
    """Compare this account's leads with Foursquare about monthly: closed ones are removed (untouched) or flagged
    "May be closed"; a different phone number becomes Other phone."""
    stats = {"checked": 0, "closed": 0, "phones": 0}
    if not configured():
        return stats
    due = datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)
    leads = db.scalars(select(Lead).where(
        Lead.account_id == account_id, Lead.deleted_at.is_(None), Lead.zip_code != "",
        or_(Lead.fsq_checked_at.is_(None), Lead.fsq_checked_at < due)).order_by(Lead.zip_code, Lead.id).limit(limit)).all()
    indexes: dict[str, Index | None] = {}
    for lead in leads:
        if lead.zip_code not in indexes:
            indexes[lead.zip_code] = index_for(lead.zip_code)
        index = indexes[lead.zip_code]
        if index is None:
            continue  # couldn't read this ZIP: try again next time
        match = index.find(lead.business_name, lead.phone)
        had = lead.other_phone
        if match and match["closed"]:
            closed.flag_or_remove(lead, REASON)
            stats["closed"] += bool(lead.closed_reason == REASON)
        other_phone(lead, match)
        stats["phones"] += bool(lead.other_phone and not had)
        lead.fsq_checked_at = datetime.now(timezone.utc)
        stats["checked"] += 1
        db.commit()
    return stats
