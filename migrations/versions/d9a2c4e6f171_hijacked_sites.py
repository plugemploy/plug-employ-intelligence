"""remove website links taken over by gambling spam (keep the leads)

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

GAMBLING = re.compile(r"gambling|casino|slot (site|ads|game)|betting", re.I)


def upgrade() -> None:
    op.add_column('leads', sa.Column('bad_website', sa.String(length=300), server_default='', nullable=False))
    op.add_column('leads', sa.Column('bad_website_why', sa.String(length=150), server_default='', nullable=False))
    # Existing leads: website text already read, or Claude's score reason mentions gambling.
    from app import website
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, website_text, score_reason FROM leads WHERE website <> '' "
        "AND (website_text <> '' OR score_reason ~* '(gambling|casino|slot|betting)')")).all()
    for r in rows:
        if website.looks_hijacked(r.website_text or "") or GAMBLING.search(r.score_reason or ""):
            conn.execute(sa.text(
                "UPDATE leads SET bad_website = left(website, 300), bad_website_why = 'it showed gambling ads', "
                "website = '', website_text = '', website_about_url = '', website_status = 'no_site', "
                "dead_since = NULL, down_since = NULL WHERE id = :id"), dict(id=r.id))


def downgrade() -> None:
    op.execute("UPDATE leads SET website = bad_website WHERE bad_website <> '' AND website = ''")
    op.drop_column('leads', 'bad_website_why')
    op.drop_column('leads', 'bad_website')
