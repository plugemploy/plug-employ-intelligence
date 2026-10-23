"""Results: what the tool did for each account (My results) and across beta testers (Beta results).

Counts only leads that aren't deleted. "Reached out" means the lead moved past New, or Gmail saw an email.
"""

from datetime import datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models import ApiUsage, FollowUp, Lead

WORKED = ("Contacted", "Interested", "Won", "Not interested")


def metrics(db: Session, account_ids: list[int], since: datetime | None = None) -> dict[int, dict]:
    """Numbers per account. With since, only leads added (and searches run) from then on."""
    ids = account_ids or [0]
    lead_q = select(
        Lead.account_id,
        func.count(),
        func.sum(case((Lead.score >= 8, 1), else_=0)),
        func.sum(case((or_(Lead.status.in_(WORKED), Lead.last_emailed_at.is_not(None)), 1), else_=0)),
        func.sum(case((Lead.status.in_(("Interested", "Won")), 1), else_=0)),
        func.sum(case((Lead.status == "Won", 1), else_=0)),
        func.sum(case((Lead.status == "Not interested", 1), else_=0)),
        func.coalesce(func.sum(case((Lead.status == "Won", Lead.deal_value), else_=0)), 0),
    ).where(Lead.account_id.in_(ids), Lead.deleted_at.is_(None))
    search_q = select(ApiUsage.account_id, func.count()).where(
        ApiUsage.account_id.in_(ids), ApiUsage.service == "google_places_text_search")
    if since:
        lead_q = lead_q.where(Lead.created_at >= since)
        search_q = search_q.where(ApiUsage.created_at >= since)
    leads = {r[0]: r[1:] for r in db.execute(lead_q.group_by(Lead.account_id))}
    searches = dict(db.execute(search_q.group_by(ApiUsage.account_id)).all())
    follow_ups = dict(db.execute(
        select(FollowUp.account_id, func.count()).where(FollowUp.account_id.in_(ids), FollowUp.done.is_(True))
        .group_by(FollowUp.account_id)).all()) if not since else {}
    out = {}
    for aid in account_ids:
        found, hot, reached, interested, won, lost, value = leads.get(aid, (0, 0, 0, 0, 0, 0, 0))
        out[aid] = dict(searches=searches.get(aid, 0), found=found or 0, hot=hot or 0, reached=reached or 0,
                        interested=interested or 0, won=won or 0, lost=lost or 0, value=float(value or 0),
                        follow_ups_done=follow_ups.get(aid, 0))
    return out


def total(rows: list[dict]) -> dict:
    """Add up several accounts' metrics, with the rates worked out from the totals."""
    keys = ("searches", "found", "hot", "reached", "interested", "won", "lost", "value", "follow_ups_done")
    t = {k: sum(r[k] for r in rows) for k in keys}
    return with_rates(t)


def with_rates(m: dict) -> dict:
    decided = m["won"] + m["lost"]
    return {**m,
            "win_rate": round(100 * m["won"] / decided) if decided else None,
            "value_per_lead_reached": round(m["value"] / m["reached"]) if m["reached"] else None,
            "avg_deal": round(m["value"] / m["won"]) if m["won"] else None}
