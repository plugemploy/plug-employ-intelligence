"""New in your territory: businesses that appear in a ZIP code in Overture's newest monthly release.

For each ZIP an account covers, the set of Overture place ids is saved per release (shared by all accounts).
When a new release arrives, places missing from the previous release's set that match the account's business
types (and pass the usual quality checks) are added as leads with a "New in your territory" reason. The first
release seen for a ZIP is only a baseline.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import overture, overture_types
from app.dedupe import find_duplicate
from app.models import Account, Lead, LeadSignal, NewInAreaCheck, OvertureSnapshot

MAX_PER_ZIP = 30  # new leads one ZIP can add in a month


def snapshot(db: Session, zip_code: str, release: str) -> tuple[set[str], set[str] | None, list[dict]]:
    """(ids now, ids in the previous release or None, places now). Saves this release's ids once."""
    places = overture.fetch_places(zip_code)
    now = {p["id"] for p in places}
    snaps = db.scalars(select(OvertureSnapshot).where(OvertureSnapshot.zip_code == zip_code)
                       .order_by(OvertureSnapshot.release.desc())).all()
    if not any(s.release == release for s in snaps):
        db.add(OvertureSnapshot(zip_code=zip_code, release=release, place_ids=",".join(sorted(now))))
        for old in snaps[1:]:  # keep this release and the one before
            db.delete(old)
        db.commit()
    before = next((s for s in snaps if s.release < release), None)
    return now, (set(before.place_ids.split(",")) if before and before.place_ids else None), places


def check_account(db: Session, account: Account) -> dict:
    """Add this month's new businesses in the account's ZIP codes. Runs nightly; does real work once a release."""
    stats = {"zips": 0, "added": 0}
    if not account.zip_list or not account.industry_list:
        return stats
    release = overture.latest_release()
    matchers = [(lambda c, w: (c, overture_types.name_pattern(w)))(*overture_types.matcher(t)) for t in account.industry_list]
    for zip_code in account.zip_list:
        done = db.scalar(select(NewInAreaCheck).where(NewInAreaCheck.account_id == account.id,
                                                      NewInAreaCheck.zip_code == zip_code))
        if done and done.release == release:
            continue
        now, before, places = snapshot(db, zip_code, release)
        stats["zips"] += 1
        added = 0
        if before is not None:  # a baseline month adds nothing
            fresh = [p for p in places if p["id"] not in before]
            for p in sorted(fresh, key=lambda p: (bool(p["brand"]), -p["confidence"])):
                if added >= MAX_PER_ZIP:
                    break
                phone, website = (p["phones"][0] if p["phones"] else ""), overture._website(p["websites"])
                if (p["operating_status"] in overture.CLOSED or p["confidence"] < overture.MIN_CONFIDENCE
                        or not (phone or website) or not any(overture.matches(p, c, pat) for c, pat in matchers)):
                    continue
                if find_duplicate(db, account.id, p["name"], zip_code, phone, website):
                    continue
                industry = next(t for t, (c, pat) in zip(account.industry_list, matchers) if overture.matches(p, c, pat))
                lead = Lead(account_id=account.id, overture_id=p["id"], business_name=p["name"][:300],
                            address=p["address"][:300], zip_code=zip_code, phone=phone[:40], website=website[:300],
                            email=(p["emails"][0] if p["emails"] else "")[:300], brand=p["brand"][:200],
                            source_confidence=p["confidence"], industry=industry, status="New")
                db.add(lead)
                db.flush()
                db.add(LeadSignal(account_id=account.id, lead_id=lead.id, kind="new_in_area",
                                  text=f"Newly listed in {zip_code} this month."))
                added += 1
        if done:
            done.release = release
        else:
            db.add(NewInAreaCheck(account_id=account.id, zip_code=zip_code, release=release))
        db.commit()
        stats["added"] += added
    return stats
