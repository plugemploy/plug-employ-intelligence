"""One-time test: how fast and how heavy is reading Foursquare's open places for a few ZIP codes, and how many of
Overture's places there does Foursquare mark closed or stale? Changes nothing in the app or its database.

Run:  .venv/bin/python scripts/fsq_test.py 75032 75087 75189
Needs FOURSQUARE_TOKEN in .env (Places Portal > OS Places > Access Data).
"""

import resource
import sys
import time
from datetime import date, timedelta

import duckdb

from app import overture
from app.config import FOURSQUARE_TOKEN
from app.dedupe import _name_key, _phone_key

CATALOG = "https://catalog.h3-hub.foursquare.com/iceberg"
STALE = date.today() - timedelta(days=365)


def peak_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024)


def connect():
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL iceberg; LOAD iceberg; SET memory_limit='400MB'; SET threads=2;")
    con.execute("CREATE SECRET fsq (TYPE iceberg, TOKEN ?)", [FOURSQUARE_TOKEN])
    con.execute(f"ATTACH 'places' AS fsq (TYPE iceberg, SECRET fsq, ENDPOINT '{CATALOG}')")
    return con


def main(zips: list[str]) -> None:
    if not FOURSQUARE_TOKEN:
        sys.exit("FOURSQUARE_TOKEN is empty in .env.")
    t = time.time()
    con = connect()
    print(f"Connected in {time.time() - t:.0f}s. Tables: {[r[0] for r in con.execute('SHOW ALL TABLES').fetchall()][:10]}")
    for z in zips:
        west, east, south, north = overture.zip_box(z)
        t = time.time()
        rows = con.execute("""
            SELECT name, tel, website, postcode, date_refreshed, date_closed
            FROM fsq.datasets.places_os
            WHERE country = 'US' AND longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ? AND postcode = ?""",
            [west, east, south, north, z]).fetchall()
        secs = time.time() - t
        closed = sum(1 for r in rows if r[5])
        stale = sum(1 for r in rows if not r[5] and r[4] and date.fromisoformat(str(r[4])[:10]) < STALE)
        print(f"\nZIP {z}: {len(rows)} Foursquare places in {secs:.0f}s (peak memory so far {peak_mb():.0f} MB); "
              f"{closed} marked closed, {stale} not confirmed in a year")
        by_phone = {_phone_key(r[1] or ""): r for r in rows if _phone_key(r[1] or "")}
        by_name = {_name_key(r[0] or ""): r for r in rows if r[0]}
        ov = [p for p in overture.fetch_places(z) if overture._reliable(p)]
        match = closed_m = stale_m = phone_diff = 0
        for p in ov:
            phone = _phone_key(p["phones"][0]) if p["phones"] else ""
            f = by_phone.get(phone) or by_name.get(_name_key(p["name"]))
            if not f:
                continue
            match += 1
            closed_m += bool(f[5])
            stale_m += bool(not f[5] and f[4] and date.fromisoformat(str(f[4])[:10]) < STALE)
            phone_diff += bool(phone and _phone_key(f[1] or "") and _phone_key(f[1]) != phone)
        print(f"  Overture usable places: {len(ov)}; also in Foursquare: {match}; of those Foursquare says closed: "
              f"{closed_m}, stale: {stale_m}; different phone number: {phone_diff}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["75032", "75087", "75189"])
