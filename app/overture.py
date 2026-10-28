"""Find leads in Overture Maps' open places data (https://overturemaps.org).

Overture's places are published under open licenses (CDLA-Permissive 2.0, Apache 2.0, CC0) that allow storing
the data and sharing it with customers, unlike Google Places content. Each release is a set of Parquet files on
a public S3 bucket; DuckDB reads only the part around one ZIP code (no download of the whole dataset).

A search = one business type in one ZIP code. Places in a ZIP are fetched once and reused for 12 hours (the data
only changes with a new monthly release), so searches in the same ZIP read Overture once and never wait in line.
The same read keeps reliable businesses a few miles around the ZIP: when a ZIP has few of a business type, the
search fills up with the closest ones, marked Nearby (roofers, plumbers and shops serve more than one ZIP).
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
FILL_TO = 15  # a search with fewer good matches in the ZIP adds nearby ones up to this many
NEARBY_MAX_MILES = 10  # nearby reach: 2.5x the ZIP's radius, 3 to 10 miles (small city ZIPs are dense)
NEARBY_ROWS = 25_000  # most nearby places kept per ZIP read (closest first), to cap memory in big cities
CACHE_ZIPS = 20  # ZIP reads kept in memory
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


def nearby_miles(zip_code: str) -> float:
    found = _zips().get(zip_code)
    if not found:
        return 0.0
    return min(NEARBY_MAX_MILES, max(3.0, 2.5 * math.sqrt(max(found[2], 0.1) / math.pi)))


def zip_box(zip_code: str) -> tuple[float, float, float, float] | None:
    """(west, east, south, north) generously around a ZIP code and its nearby reach; the postcode filter trims it."""
    found = _zips().get(zip_code)
    if not found:
        return None
    lat, lon, sq_mi = found
    half = max(0.05, math.sqrt(max(sq_mi, 1)) / 69 * 1.2, nearby_miles(zip_code) / 69)  # degrees of latitude; ~69 miles each
    lon_half = half / max(0.2, math.cos(math.radians(lat)))
    return lon - lon_half, lon + lon_half, lat - half, lat + half


def miles_from(zip_code: str, lat: float, lon: float) -> float:
    zlat, zlon, _ = _zips()[zip_code]
    return math.hypot((lat - zlat) * 69, (lon - zlon) * 69 * math.cos(math.radians(zlat)))


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
    return [p for p in fetch_area(zip_code) if p["postcode"] == zip_code]


def fetch_nearby(zip_code: str) -> list[dict]:
    """Reliable places outside the ZIP within its nearby reach, closest first, each with `miles`."""
    reach, out = nearby_miles(zip_code), []
    for p in fetch_area(zip_code):
        if p["postcode"] != zip_code and p["lat"] is not None:
            miles = miles_from(zip_code, p["lat"], p["lon"])
            if miles <= reach:
                out.append({**p, "miles": miles})
    return sorted(out, key=lambda p: p["miles"])


def fetch_area(zip_code: str) -> list[dict]:
    """The ZIP's places plus reliable ones around it, read once per release and kept for 12 hours."""
    if zip_box(zip_code) is None:
        return []
    release = latest_release()
    key = (release, zip_code)
    if (hit := _cached(key)) is not None:  # already read: no waiting in line
        return hit
    with _lock:
        if (hit := _cached(key)) is not None:  # read by someone else while this one waited
            return hit
        places = _read_area(release, zip_code)
        with _cache_guard:
            if len(_zip_cache) >= CACHE_ZIPS:
                _zip_cache.clear()
            _zip_cache[key] = (time.time(), places)
        return places


def _read_area(release: str, zip_code: str) -> list[dict]:
    west, east, south, north = zip_box(zip_code)
    zlat, zlon, _ = _zips()[zip_code]
    lon_scale = math.cos(math.radians(zlat))
    path = f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*.parquet"
    closed = ", ".join(f"'{c}'" for c in CLOSED)
    rows = _connect().execute(f"""
        SELECT id, names.primary, taxonomy.primary, basic_category, confidence, operating_status,
               phones, websites, emails, CASE WHEN brand.wikidata IS NOT NULL THEN brand.names.primary END,
               addresses[1].freeform, left(addresses[1].postcode, 5) AS zip, addresses[1].locality,
               bbox.ymin, bbox.xmin
        FROM read_parquet('{path}')
        WHERE bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ?
          AND (zip = ? OR (confidence >= ? AND coalesce(operating_status, 'open') NOT IN ({closed})
                           AND (coalesce(len(phones), 0) > 0 OR coalesce(len(websites), 0) > 0)))
        ORDER BY zip IS NOT DISTINCT FROM ? DESC, (bbox.ymin - ?) ^ 2 + ((bbox.xmin - ?) * ?) ^ 2
        LIMIT ?""", [west, east, south, north, zip_code, MIN_CONFIDENCE, zip_code, zlat, zlon, lon_scale,
                     NEARBY_ROWS]).fetchall()
    return [dict(id=r[0], name=r[1] or "", category=r[2], basic_category=r[3], confidence=r[4] or 0,
                 operating_status=r[5], phones=r[6] or [], websites=r[7] or [], emails=r[8] or [],
                 brand=r[9] or "", address=r[10] or "", postcode=r[11] or "", city=r[12] or "", lat=r[13], lon=r[14])
            for r in rows]


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
    """Search one business type in one ZIP and save the reliable new ones as New leads. When the ZIP has fewer
    than FILL_TO good matches, the closest ones nearby are added too (marked Nearby)."""
    stats = {"zip": zip_code, "industry": industry, "requests": 1, "found": 0, "added": 0, "added_nearby": 0,
             "skipped_closed": 0, "skipped_unreliable": 0, "skipped_duplicate": 0, "skipped_over_limit": 0}
    check_search(db, account_id)
    cats, words = overture_types.matcher(industry)
    pattern = overture_types.name_pattern(words)
    found = [p for p in fetch_places(zip_code) if matches(p, cats, pattern)]
    stats["found"] = len(found)
    db.add(ApiUsage(account_id=account_id, service=SERVICE, detail=f"{industry} in {zip_code}", est_cost_usd=0))
    db.commit()
    known = set(db.scalars(select(Lead.overture_id).where(Lead.account_id == account_id, Lead.overture_id.is_not(None))))
    # Independents before chains (a listed brand), then the most confident: local owners buy local ads.
    good = 0
    for p in sorted(found, key=lambda p: (bool(p["brand"]), -p["confidence"])):
        if _consider(db, account_id, p, zip_code, industry, known, stats, ""):
            good += 1
    if good < FILL_TO:
        nearby = [p for p in fetch_nearby(zip_code) if matches(p, cats, pattern)]
        for p in sorted(nearby, key=lambda p: (bool(p["brand"]), p["miles"])):
            if good >= FILL_TO:
                break
            town = (p["city"] or "").title() or (f"ZIP {p['postcode']}" if p["postcode"] else "nearby")
            before = stats["added"]
            _consider(db, account_id, p, p["postcode"] or zip_code, industry, known, stats,
                      f"{town}, {max(1, round(p['miles']))} mi from {zip_code}"[:120])
            good += stats["added"] - before  # nearby ones already saved don't count: the next closest come next time
    db.commit()
    return stats


def _consider(db: Session, account_id: int, p: dict, zip_code: str, industry: str, known: set, stats: dict,
              nearby: str) -> bool:
    """Save one matching place as a lead if it's reliable and new. True when it's a good match (new or known)."""
    phone, website = (p["phones"][0] if p["phones"] else ""), _website(p["websites"])
    if p["operating_status"] in CLOSED:
        stats["skipped_closed"] += 1
        return False
    if p["confidence"] < MIN_CONFIDENCE or not (phone or website):
        stats["skipped_unreliable"] += 1
        return False
    if p["id"] in known:
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
                    source_confidence=p["confidence"], industry=industry, status="New", nearby=nearby))
        db.flush()  # so find_duplicate sees it for the next place
        known.add(p["id"])
        stats["added"] += 1
        stats["added_nearby"] += bool(nearby)
    return True


# How many businesses each type could bring, shown beside the types on the setup page.
ESTIMATE_ZIPS = 5  # ZIPs counted (the first ones, read in the background)
_warming: set[str] = set()
_counts: dict[tuple[str, str], dict[str, tuple[set, set]]] = {}  # (release, zip) -> type -> (ids here, ids nearby)


def _reliable(p: dict) -> bool:
    return (p["operating_status"] not in CLOSED and p["confidence"] >= MIN_CONFIDENCE
            and bool(p["phones"] or _website(p["websites"])))


def _zip_counts(zip_code: str, types: list[str]) -> dict[str, tuple[set, set]]:
    key = (latest_release(), zip_code)
    out = _counts.get(key, {})
    missing = [t for t in types if t not in out]
    if missing:
        here = [p for p in fetch_places(zip_code) if _reliable(p)]
        near = [p for p in fetch_nearby(zip_code) if _reliable(p)]
        for t in missing:
            cats, words = overture_types.matcher(t)
            pat = overture_types.name_pattern(words)
            out[t] = ({p["id"] for p in here if matches(p, cats, pat)}, {p["id"] for p in near if matches(p, cats, pat)})
        if key not in _counts and len(_counts) >= CACHE_ZIPS:
            _counts.clear()
        _counts[key] = out
    return out


def ready(zip_code: str) -> bool:
    """Is this ZIP's area already read (so counting is instant)?"""
    try:
        return zip_box(zip_code) is None or _cached((latest_release(), zip_code)) is not None
    except Exception:
        return False


def warm(zips: list[str]) -> None:
    """Read these ZIPs' areas in the background (one thread, one ZIP at a time), so counts and searches are quick."""
    todo = [z for z in zips if z not in _warming and not ready(z)]
    if not todo:
        return
    _warming.update(todo)

    def run():
        for z in todo:
            try:
                fetch_area(z)
            except Exception:
                pass  # counts just won't show for this ZIP
            finally:
                _warming.discard(z)
    threading.Thread(target=run, daemon=True).start()


def estimates(zips: list[str], types: list[str]) -> dict[str, tuple[int, int]] | None:
    """type -> (businesses in these ZIPs, more nearby), or None while the ZIPs are still being read."""
    zips = [z for z in zips if zip_box(z) is not None][:ESTIMATE_ZIPS]
    if not all(ready(z) for z in zips):
        warm(zips)
        return None
    per_zip = [_zip_counts(z, types) for z in zips]
    out = {}
    for t in types:
        here = set().union(*(c[t][0] for c in per_zip)) if per_zip else set()
        near = set().union(*(c[t][1] for c in per_zip)) - here if per_zip else set()
        out[t] = (len(here), len(near))
    return out


def waiting(zips: list[str], industry: str, types: list[str], have: int) -> tuple[int, int] | None:
    """For the free search offer: (more of this type in and around the ZIPs than the `have` leads already shown,
    other businesses of any listed type), or None while the area is still being read."""
    zips = [z for z in zips if zip_box(z) is not None][:ESTIMATE_ZIPS]
    if not zips or not all(ready(z) for z in zips):
        warm(zips)
        return None
    kinds = list(dict.fromkeys(types + [industry]))
    per_zip = [_zip_counts(z, kinds) for z in zips]
    ids = lambda t: set().union(*(c[t][0] | c[t][1] for c in per_zip))
    mine = ids(industry)
    everyone = set().union(*(ids(t) for t in kinds))
    return max(0, len(mine) - have), len(everyone - mine)
