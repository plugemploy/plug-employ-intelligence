"""flag leads whose website was taken over by gambling spam

Revision ID: d9a2c4e6f171
Revises: c3f7a1d9e482
Create Date: 2026-11-11 17:00:00.000000

"""
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9a2c4e6f171'
down_revision: Union[str, Sequence[str], None] = 'c3f7a1d9e482'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Flag existing leads (website text already read, or Claude's score reason mentions gambling)."""
    from app import closed, website
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, website_text, score_reason FROM leads WHERE closed_reason = '' AND open_confirmed_at IS NULL "
        "AND status <> 'Sold' AND (website_text <> '' OR score_reason ~* '(gambling|casino|slot|betting)')")).all()
    for r in rows:
        if website.looks_hijacked(r.website_text or "") or (
                r.score_reason and re.search(r"gambling|casino|slot (site|ads|game)|betting", r.score_reason, re.I)):
            conn.execute(sa.text("UPDATE leads SET closed_reason = :why, closed_flagged_at = now(), "
                                 "website_status = 'dead_hijacked', closed_checked_at = now() WHERE id = :id"),
                         dict(why=closed.REASONS["dead_hijacked"], id=r.id))


def downgrade() -> None:
    op.execute("UPDATE leads SET closed_reason = '', website_status = 'ok' WHERE website_status = 'dead_hijacked'")
