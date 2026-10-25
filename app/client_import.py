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
from app.website import full_url
from app.models import Lead

MAX_ROWS = 2000
# Our field -> words a column heading may use (compared lower case, punctuation removed).
# A field may match several columns (e.g. Contact 1 / Contact 2): the first non-empty one in a row is used.
HEADINGS = {
    "name": ["business name", "company name", "client name", "customer name", "account name", "advertiser",
             "business", "company", "client", "customer", "name"],
    "phone": ["phone", "phone number", "business phone", "telephone", "sponsor index phone", "contact 1 phone",
              "contact 1 alt phone", "contact 2 phone", "mobile", "cell"],
    "email": ["email", "business email", "billing email", "contact 1 email", "contact 2 email"],
    "contact_name": ["contact name", "primary contact", "owner", "owner name", "contact 1 name", "contact 2 name"],
    "website": ["website", "web site", "url", "site", "web"],
    "address": ["address", "street", "street address", "address 1", "address line 1", "contact 1 address",
                "contact 2 address"],
    "zip_code": ["zip", "zip code", "zipcode", "postal code", "postcode", "contact 1 zip", "contact 2 zip"],
    "status": ["status", "client status", "account status"],
    "industry": ["industry", "business type", "category", "type", "vertical"],
    "mrr": ["monthly", "monthly amount", "mrr", "monthly recurring revenue", "monthly rate", "avg monthly amount",
            "average monthly amount", "rate", "price per month", "monthly price", "amount", "monthly ad spend"],
    "months_in_print": ["months in print", "months active", "months as client", "months as a client"],
    "remaining_months": ["remaining months in print", "remaining months", "months remaining", "months left"],
    "contract_months": ["contract length", "contract months", "months", "term", "term months", "length",
                        "contract term"],
    "ad_mgmt_fee": ["ad management fee", "management fee", "ad mgmt fee", "setup fee", "one time fee"],
    "start_date": ["start date", "start", "signed", "signed date", "date signed", "sold date", "contract start",
                   "start of contract", "date"],
}


def _clean(heading: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", heading.lower().replace("_", " ")).split())


def map_columns(headings: list[str]) -> dict[str, list[int]]:
    """Our field -> column numbers, best-matching heading first. A column is used for one field only."""
    cleaned = [_clean(h) for h in headings]
    out: dict[str, list[int]] = {}
    taken: set[int] = set()
    for field, words in HEADINGS.items():
        cols = []
        for word in words:  # earlier words are better matches
            cols += [i for i, h in enumerate(cleaned) if h == word and i not in taken and i not in cols]
        if cols:
            out[field] = cols
            taken.update(cols)
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
    return n if 0 <= n <= 240 else None


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
    # Exports often start with a title line or two: the headings are the first row naming a business column.
    start = next((i for i, r in enumerate(all_rows[:15]) if "name" in map_columns(r)), None)
    if start is None:
        return [], {}, ["Couldn't find a column for the business name. Name one column \"Business name\"."]
    headings, body = all_rows[start], all_rows[start + 1:]
    cols = map_columns(headings)
    problems = []
    if len(body) > MAX_ROWS:
        problems.append(f"Only the first {MAX_ROWS} rows are imported.")
        body = body[:MAX_ROWS]
    def get(r: list[str], field: str) -> str:
        return next((r[i].strip() for i in cols.get(field, []) if i < len(r) and r[i].strip()), "")

    rows, inactive = [], 0
    for r in body:
        name = get(r, "name")
        if not name:
            continue
        if re.search(r"inactive|cancel|former|lost|closed|expired|past", get(r, "status").lower()):
            inactive += 1
            continue
        zip_code = (re.search(r"\b\d{5}\b", get(r, "zip_code") or get(r, "address")) or [""])[0]
        rows.append(dict(name=name[:300], phone=get(r, "phone")[:40], website=full_url(get(r, "website"))[:300],
                         email=get(r, "email")[:300] if "@" in get(r, "email") else "",
                         contact_name=get(r, "contact_name")[:200],
                         address=get(r, "address")[:300], zip_code=zip_code, industry=get(r, "industry")[:100],
                         mrr=_money(get(r, "mrr")), contract_months=_months(get(r, "contract_months")),
                         months_in_print=_months(get(r, "months_in_print")),
                         remaining_months=_months(get(r, "remaining_months")),
                         ad_mgmt_fee=_money(get(r, "ad_mgmt_fee")), start_date=_date(get(r, "start_date"))))
    if inactive:
        problems.append(f"Skipped {inactive} rows whose status isn't active.")
    found = {f: headings[i[0]] + (f" (+{len(i) - 1} more)" if len(i) > 1 else "") for f, i in cols.items()}
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
                        email=r["email"], contact_name=r["contact_name"], address=r["address"],
                        zip_code=r["zip_code"], industry=r["industry"], status="New")
            db.add(lead)
            db.flush()
            stats["added"] += 1
        else:
            stats["marked_sold"] += 1
            lead.deleted_at = None  # a client someone had deleted comes back
            for field in ("phone", "website", "email", "contact_name", "address", "zip_code", "industry"):
                if r[field] and not getattr(lead, field):
                    setattr(lead, field, r[field])
        start = r["start_date"]
        if start is None and r["months_in_print"]:  # e.g. "Months in Print": a client for that many months
            start = add_months(now.date(), -r["months_in_print"])
        months = r["contract_months"]
        if months is None and (r["months_in_print"] or r["remaining_months"]):
            months = (r["months_in_print"] or 0) + (r["remaining_months"] or 0) or None
        when = datetime.combine(start, time(12), tzinfo=timezone.utc) if start else now
        lead.status, lead.source, lead.found_elsewhere = "Sold", "import", "yes"
        lead.outcome_at = lead.outcome_at or when
        lead.reach("Sold", when)
        lead.mrr = r["mrr"] if r["mrr"] is not None else lead.mrr
        lead.contract_months = months or lead.contract_months
        lead.ad_mgmt_fee = r["ad_mgmt_fee"] if r["ad_mgmt_fee"] is not None else lead.ad_mgmt_fee
        if r["remaining_months"] is not None:
            end = add_months(now.date(), r["remaining_months"])
        elif lead.contract_months:
            end = add_months(start or now.date(), lead.contract_months)
        else:
            end = None
        if end and end >= date.today():
            set_renewal(db, lead, end)
    db.commit()
    return stats
