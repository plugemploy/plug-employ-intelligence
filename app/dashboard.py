"""Dashboard: a donut of leads by status. Clicking a slice lists those leads."""

import math

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import STATUSES, Lead

# Checked for color-blind readers (every pair stays apart under protan/deutan/tritan simulation).
COLORS = {"New": "#cbd5e1", "Contacted": "#2563eb", "Appointment set": "#fbbf24",
          "Appointment shown": "#6b21a8", "Sold": "#10b981", "Not interested": "#b91c1c"}
LABELS = {"New": "New (not contacted)"}
SIZE, RADIUS, HOLE = 240, 110, 68  # donut drawn in a SIZE x SIZE box


def counts(db: Session, account_id: int) -> list[dict]:
    """One row per status (in pipeline order), with count, share and the donut slice to draw."""
    found = dict(db.execute(select(Lead.status, func.count()).where(
        Lead.account_id == account_id, Lead.deleted_at.is_(None)).group_by(Lead.status)).all())
    total = sum(found.values())
    rows, start = [], 0.0
    for status in STATUSES:
        n = found.get(status, 0)
        share = n / total if total else 0
        rows.append({"status": status, "label": LABELS.get(status, status), "count": n, "color": COLORS[status],
                     "pct": round(100 * share), "path": _slice(start, start + share) if n else ""})
        start += share
    return rows


def _point(fraction: float, r: float) -> tuple[float, float]:
    angle = 2 * math.pi * fraction - math.pi / 2  # start at 12 o'clock, go clockwise
    c = SIZE / 2
    return c + r * math.cos(angle), c + r * math.sin(angle)


def _slice(a: float, b: float) -> str:
    """SVG path for one ring segment from fraction a to b."""
    if b - a >= 0.9999:  # a single status: two half rings, since one arc can't close a full circle
        return _slice(0, 0.5) + " " + _slice(0.5, 1.0)
    large = 1 if b - a > 0.5 else 0
    (x1, y1), (x2, y2) = _point(a, RADIUS), _point(b, RADIUS)
    (x3, y3), (x4, y4) = _point(b, HOLE), _point(a, HOLE)
    return (f"M{x1:.2f},{y1:.2f} A{RADIUS},{RADIUS} 0 {large} 1 {x2:.2f},{y2:.2f} "
            f"L{x3:.2f},{y3:.2f} A{HOLE},{HOLE} 0 {large} 0 {x4:.2f},{y4:.2f} Z")


def leads_in(db: Session, account_id: int, status: str, limit: int = 50) -> tuple[list[Lead], int]:
    q = select(Lead).where(Lead.account_id == account_id, Lead.deleted_at.is_(None), Lead.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    leads = db.scalars(q.order_by(Lead.updated_at.desc()).limit(limit)).all()
    return leads, total


def sales_missing_amounts(db: Session, account_id: int) -> int:
    """Sold leads without a monthly amount or contract length, so results would undercount them."""
    return db.scalar(select(func.count()).select_from(Lead).where(
        Lead.account_id == account_id, Lead.deleted_at.is_(None), Lead.status == "Sold",
        (Lead.mrr.is_(None)) | (Lead.contract_months.is_(None)))) or 0
