"""Results: the sales steps and money each account closed (My results), and across beta testers (Beta results).

Counts leads that aren't deleted. Each step counts leads that reached it (kept even if they later said no);
"contacted" also counts leads Gmail saw an email to. Money comes from Sold leads.
"""

from datetime import datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models import Lead

KEYS = ("contacted", "appts_set", "appts_shown", "sold", "mrr", "contract_value", "ad_fees", "found_only")


def metrics(db: Session, account_ids: list[int], since: datetime | None = None) -> dict[int, dict]:
    """Numbers per account. With since, only steps reached (and sales made) from then on."""
    ids = account_ids or [0]

    def reached(col, extra=None):
        cond = col.is_not(None) if since is None else col >= since
        if extra is not None:
            cond = or_(cond, extra)
        return func.sum(case((cond, 1), else_=0))

    emailed = Lead.last_emailed_at.is_not(None) if since is None else Lead.last_emailed_at >= since
    sold = Lead.sold_at.is_not(None) if since is None else Lead.sold_at >= since
    rows = db.execute(select(
        Lead.account_id,
        reached(Lead.contacted_at, emailed),
        reached(Lead.appt_set_at),
        reached(Lead.appt_shown_at),
        reached(Lead.sold_at),
        func.coalesce(func.sum(case((sold, Lead.mrr), else_=0)), 0),
        func.coalesce(func.sum(case((sold, Lead.mrr * func.coalesce(Lead.contract_months, 0)), else_=0)), 0),
        func.coalesce(func.sum(case((sold, Lead.ad_mgmt_fee), else_=0)), 0),
        func.sum(case((sold & (Lead.found_elsewhere == "no"), 1), else_=0)),  # sales they wouldn't have found otherwise
    ).where(Lead.account_id.in_(ids), Lead.deleted_at.is_(None)).group_by(Lead.account_id))
    found = {r[0]: r[1:] for r in rows}
    out = {}
    for aid in account_ids:
        vals = found.get(aid, (0,) * len(KEYS))
        out[aid] = {k: (float(v or 0) if k in ("mrr", "contract_value", "ad_fees") else int(v or 0))
                    for k, v in zip(KEYS, vals)}
    return out


def total(rows: list[dict]) -> dict:
    """Add up several accounts' metrics."""
    return {k: sum(r[k] for r in rows) for k in KEYS}
