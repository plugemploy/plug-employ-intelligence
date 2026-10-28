"""Find leads in Overture Maps' open places data (https://overturemaps.org).

Overture's places are published under open licenses (CDLA-Permissive 2.0, Apache 2.0, CC0) that allow storing
the data and sharing it with customers, unlike Google Places content. Each release is a set of Parquet files on
a public S3 bucket; DuckDB reads only the part around one ZIP code (no download of the whole dataset).

A search = one business type in one ZIP code. Places in a ZIP are fetched once and reused for 12 hours (the data
only changes with a new monthly release), so searches in the same ZIP read Overture once and never wait in line.
"""

import csv
import math
import re
import threading
import time
from functools import lru_cache
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import overture_types
from app.config import OVERTURE_PARALLEL
from app.dedupe import find_duplicate
from app.gmail import site_domain
from app.models import ApiUsage, Lead
from app.places import check_search

SERVICE = "overture_search"  # how a search is logged in api_usage (it costs nothing)
BUCKET = "https://overturemaps-us-west-2.s3.amazonaws.com"
MAX_ADDED = 30  # most new leads one search adds (independents first), so lists and scoring stay manageable
MIN_CONFIDENCE = 0.2  # Overture's 0-1 confidence the place is real; many real small businesses sit at 0.3-0.4
CLOSED = {"permanently_closed", "temporarily_closed", "closed"}
SOCIAL = {"facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "tiktok.com",
          "yelp.com", "google.com", "linktr.ee"}
CACHE_SECONDS = 12 * 3600
ZIP_FILE = Path(__file__).parent / "data" / "zip_centroids.csv"  # US Census ZCTA centers (public domain)

_lock = threading.BoundedSemaphore(OVERTURE_PARALLEL)  # Overture reads at once (memory: ~500 MB each)
_cache_guard = threading.Lock()
_zip_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_release: dict = {"name": "", "at": 0.0}


@lru_cache(maxsize=1)
def _zips() -> dict[str, tuple[float, float, float]]:
    with ZIP_FILE.open() as f:
        return {r["zip"]: (float(r["lat"]), float(r["lon"]), float(r["land_sq_mi"])) for r in csv.DictReader(f)}


def zip_box(zip_code: str) -> tuple[float, float, float, float] | None:
    """(west, east, south, north) generously around a ZIP code; the postcode filter trims it."""
    found = _zips().get(zip_code)
    if not found:
        return None
    lat, lon, sq_mi = found
    half = max(0.05, math.sqrt(max(sq_mi, 1)) / 69 * 1.2)  # degrees of latitude; ~69 miles each
    lon_half = half / max(0.2, math.cos(math.radians(lat)))
    return lon - lon_half, lon + lon_half, lat - half, lat + half


def latest_release() -> str:
    """The newest Overture release folder, e.g. 2026-10-21.0 (checked twice a day)."""
    if _release["name"] and time.time() - _release["at"] < 12 * 3600:
        return _release["name"]
    r = httpx.get(f"{BUCKET}/", params={"list-type": "2", "prefix": "release/", "delimiter": "/"}, timeout=20)
    r.raise_for_status()
    names = sorted(re.findall(r"<Prefix>release/(\d{4}-\d{2}-\d{2}\.\d+)/</Prefix>", r.text))
    if not names:
        raise RuntimeError("Couldn't find an Overture release.")
    _release.update(name=names[-1], at=time.time())
    return names[-1]


def _connect():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2'; SET memory_limit='300MB'; SET threads=2;")
    return con


def fetch_places(zip_code: str) -> list[dict]:
    """Every Overture place whose address is in this ZIP code. Kept separate so tests can replace it.
    `brand` is set only for chains: a brand with a Wikidata entry (Overture also names brands of single shops)."""
    box = zip_box(zip_code)
    if box is None:
        return []
    release = latest_release()
    key = (release, zip_code)
    if (hit := _cached(key)) is not None:  # already read: no waiting in line
        return hit
    with _lock:
        if (hit := _cached(key)) is not None:  # read by someone else while this one waited
            return hit
        west, east, south, north = box
        path = f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*.parquet"
        rows = _connect().execute(f"""
            SELECT id, names.primary, taxonomy.primary, basic_category, confidence, operating_status,
                   phones, websites, emails, CASE WHEN brand.wikidata IS NOT NULL THEN brand.names.primary END,
                   addresses[1].freeform
            FROM read_parquet('{path}')
            WHERE bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ?
              AND left(addresses[1].postcode, 5) = ?""", [west, east, south, north, zip_code]).fetchall()
        places = [dict(id=r[0], name=r[1] or "", category=r[2], basic_category=r[3], confidence=r[4] or 0,
                       operating_status=r[5], phones=r[6] or [], websites=r[7] or [], emails=r[8] or [],
                       brand=r[9] or "", address=r[10] or "") for r in rows]
        with _cache_guard:
            if len(_zip_cache) > 50:
                _zip_cache.clear()
            _zip_cache[key] = (time.time(), places)
        return places


def _cached(key: tuple[str, str]) -> list[dict] | None:
    with _cache_guard:
        hit = _zip_cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < CACHE_SECONDS else None


def matches(place: dict, cats: list[str], pattern: str) -> bool:
    """Is this place the business type? Its category decides; a name word decides only when the category is
    missing or general (so "Tree service" doesn't pick up "Pear Tree Dental")."""
    if place["category"] in cats or place["basic_category"] in cats:
        return True
    vague = (not cats or place["category"] is None or place["category"] in overture_types.GENERIC
             or place["basic_category"] in overture_types.GENERIC)
    return bool(pattern and vague and re.search(pattern, place["name"].lower()))


def _website(urls: list[str]) -> str:
    for u in urls:
        if site_domain(u.lower()) not in SOCIAL:
            return u
    return ""


def search_and_save(db: Session, account_id: int, zip_code: str, industry: str) -> dict:
    """Search one business type in one ZIP and save the reliable new ones as New leads."""
    stats = {"zip": zip_code, "industry": industry, "requests": 1, "found": 0, "added": 0, "skipped_closed": 0,
             "skipped_unreliable": 0, "skipped_duplicate": 0, "skipped_over_limit": 0}
    check_search(db, account_id)
    cats, words = overture_types.matcher(industry)
    pattern = overture_types.name_pattern(words)
    found = [p for p in fetch_places(zip_code) if matches(p, cats, pattern)]
    stats["found"] = len(found)
    db.add(ApiUsage(account_id=account_id, service=SERVICE, detail=f"{industry} in {zip_code}", est_cost_usd=0))
    db.commit()
    known = set(db.scalars(select(Lead.overture_id).where(Lead.account_id == account_id, Lead.overture_id.is_not(None))))
    # Independents before chains (a listed brand), then the most confident: local owners buy local ads.
    for p in sorted(found, key=lambda p: (bool(p["brand"]), -p["confidence"])):
        phone, website = (p["phones"][0] if p["phones"] else ""), _website(p["websites"])
        if p["operating_status"] in CLOSED:
            stats["skipped_closed"] += 1
        elif p["confidence"] < MIN_CONFIDENCE or not (phone or website):
            stats["skipped_unreliable"] += 1
        elif p["id"] in known:
            stats["skipped_duplicate"] += 1
        elif dupe := find_duplicate(db, account_id, p["name"], zip_code, phone, website):
            if not dupe.overture_id:  # an older lead (e.g. from Google) now backed by open data
                dupe.overture_id = p["id"]
            stats["skipped_duplicate"] += 1
        elif stats["added"] >= MAX_ADDED:
            stats["skipped_over_limit"] += 1
        else:
            db.add(Lead(account_id=account_id, overture_id=p["id"], business_name=p["name"][:300],
                        address=p["address"][:300], zip_code=zip_code, phone=phone[:40], website=website[:300],
                        email=(p["emails"][0] if p["emails"] else "")[:300], brand=p["brand"][:200],
                        source_confidence=p["confidence"], industry=industry, status="New"))
            db.flush()  # so find_duplicate sees it for the next place
            stats["added"] += 1
    db.commit()
    return stats
