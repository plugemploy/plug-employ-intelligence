"""other phone from the website

Revision ID: c3f7a1d9e482
Revises: b8d4f2a6c913
Create Date: 2026-11-11 15:00:00.000000

"""
from types import SimpleNamespace
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f7a1d9e482'
down_revision: Union[str, Sequence[str], None] = 'b8d4f2a6c913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('leads', sa.Column('other_phone', sa.String(length=40), server_default='', nullable=False))
    op.add_column('leads', sa.Column('phone_source', sa.String(length=20), server_default='', nullable=False))
    # Leads whose website was already read: fill Other phone from the saved text (no new website visits).
    from app import phones
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, phone, status, close_lead_id, source, website_text FROM leads "
                                "WHERE website_status = 'ok' AND website_text <> ''")).all()
    for r in rows:
        lead = SimpleNamespace(phone=r.phone or "", other_phone="", phone_source="", status=r.status,
                               close_lead_id=r.close_lead_id, source=r.source, website_text=r.website_text)
        if phones.from_site(lead):
            conn.execute(sa.text("UPDATE leads SET phone = :p, other_phone = :o, phone_source = :s WHERE id = :id"),
                         dict(p=lead.phone, o=lead.other_phone, s=lead.phone_source, id=r.id))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE leads SET phone = '' WHERE phone_source = 'website'")
    op.drop_column('leads', 'phone_source')
    op.drop_column('leads', 'other_phone')
