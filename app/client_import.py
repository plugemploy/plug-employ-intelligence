"""Import an account's current clients from a CSV so scoring learns from real customers.

Each row becomes a Sold lead: an existing lead (same phone, website, or name in the same ZIP; deleted ones are
restored) is marked Sold, anything else is added. Imported clients are labeled source="import" and left out of
results (My results, Beta results), which count sales made while using the app.
"""

import csv
import io
import re
from datetime import date, datetime, time, timezone

from sqlalchemy.orm import Session

from app.dedupe import find_duplicate
from app.models import Lead

MAX_ROWS = 2000
# Our field -> words a column heading may use (compared lower case, punctuation removed).
HEADINGS = {
    "name": ["business name", "business", "company", "company name", "client", "client name", "customer",
             "customer name", "advertiser", "account", "account name", "name"],
    "phone": ["phone", "phone number", "telephone", "business phone", "mobile", "cell"],
    "website": ["website", "web site", "url", "site", "web"],
    "address": ["address", "street", "street address", "address 1", "address line 1"],
    "zip_code": ["zip", "zip code", "zipcode", "postal code", "postcode"],
    "industry": ["industry", "business type", "category", "type", "vertical"],
    "mrr": ["monthly", "monthly amount", "mrr", "monthly recurring revenue", "monthly rate", "rate",
            "price per month", "monthly price", "amount", "monthly ad spend"],
    "contract_months": ["contract length", "contract months", "months", "term", "term months", "length",
                        "contract term"],
    "ad_mgmt_fee": ["ad management fee", "management fee", "ad mgmt fee", "setup fee", "one time fee"],
    "start_date": ["start date", "start", "signed", "signed date", "date signed", "sold date", "contract start",
                   "start of contract", "date"],
}


def _clean(heading: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", heading.lower().replace("_", " ")).split())


def map_columns(headings: list[str]) -> dict[str, int]:
    """Our field -> column number, picking the best-matching heading for each field."""
    cleaned = [_clean(h) for h in headings]
    out: dict[str, int] = {}
    for field, words in HEADINGS.items():
        for word in words:  # earlier words are better matches
            hit = next((i for i, h in enumerate(cleaned) if h == word and i not in out.values()), None)
            if hit is not None:
                out[field] = hit
                break
    return out


def _money(raw: str) -> float | None:
    raw = re.sub(r"[^0-9.]", "", raw or "")
    try:
        return round(float(raw), 2) if raw else None
    except ValueError:
        return None


def _months(raw: str) -> int | None:
    m = re.search(r"\d+", raw or "")
    if not m:
        return None
    n = int(m.group())
    if "year" in (raw or "").lower() or "yr" in (raw or "").lower():
        n *= 12
    return n if 1 <= n <= 120 else None


def _date(raw: str) -> date | None:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%b %d, %Y", "%B %d, %Y", "%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse(content: bytes) -> tuple[list[dict], dict[str, str], list[str]]:
    """(rows, which heading each field came from, problems). Accepts a CSV with a heading row."""
    text = content.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    all_rows = [r for r in reader if any(c.strip() for c in r)]
    if not all_rows:
        return [], {}, ["The file is empty."]
    headings, body = all_rows[0], all_rows[1:]
    cols = map_columns(headings)
    if "name" not in cols:
        return [], {}, ["Couldn't find a column for the business name. Name one column \"Business name\"."]
    problems = []
    if len(body) > MAX_ROWS:
        problems.append(f"Only the first {MAX_ROWS} rows are imported.")
        body = body[:MAX_ROWS]
    get = lambda r, f: (r[cols[f]].strip() if f in cols and cols[f] < len(r) else "")
    rows = []
    for r in body:
        name = get(r, "name")
        if not name:
            continue
        zip_code = (re.search(r"\b\d{5}\b", get(r, "zip_code") or get(r, "address")) or [""])[0]
        rows.append(dict(name=name[:300], phone=get(r, "phone")[:40], website=get(r, "website")[:300],
                         address=get(r, "address")[:300], zip_code=zip_code, industry=get(r, "industry")[:100],
                         mrr=_money(get(r, "mrr")), contract_months=_months(get(r, "contract_months")),
                         ad_mgmt_fee=_money(get(r, "ad_mgmt_fee")), start_date=_date(get(r, "start_date"))))
    found = {f: headings[i] for f, i in cols.items()}
    return rows, found, problems


def preview(db: Session, account_id: int, rows: list[dict]) -> dict:
    matched = sum(1 for r in rows if find_duplicate(db, account_id, r["name"], r["zip_code"], r["phone"], r["website"]))
    return {"total": len(rows), "matched": matched, "new": len(rows) - matched}


def apply(db: Session, account_id: int, rows: list[dict]) -> dict:
    """Save the clients as Sold. Returns counts."""
    from app.main import add_months, set_renewal
    stats = {"marked_sold": 0, "added": 0}
    now = datetime.now(timezone.utc)
    for r in rows:
        lead = find_duplicate(db, account_id, r["name"], r["zip_code"], r["phone"], r["website"])
        if lead is None:
            lead = Lead(account_id=account_id, business_name=r["name"], phone=r["phone"], website=r["website"],
                        address=r["address"], zip_code=r["zip_code"], industry=r["industry"], status="New")
            db.add(lead)
            db.flush()
            stats["added"] += 1
        else:
            stats["marked_sold"] += 1
            lead.deleted_at = None  # a client someone had deleted comes back
            for field in ("phone", "website", "address", "zip_code", "industry"):
                if r[field] and not getattr(lead, field):
                    setattr(lead, field, r[field])
        when = datetime.combine(r["start_date"], time(12), tzinfo=timezone.utc) if r["start_date"] else now
        lead.status, lead.source, lead.found_elsewhere = "Sold", "import", "yes"
        lead.outcome_at = lead.outcome_at or when
        lead.reach("Sold", when)
        lead.mrr = r["mrr"] if r["mrr"] is not None else lead.mrr
        lead.contract_months = r["contract_months"] or lead.contract_months
        lead.ad_mgmt_fee = r["ad_mgmt_fee"] if r["ad_mgmt_fee"] is not None else lead.ad_mgmt_fee
        if lead.contract_months:
            end = add_months((r["start_date"] or now.date()), lead.contract_months)
            if end >= date.today():
                set_renewal(db, lead, end)
    db.commit()
    return stats
