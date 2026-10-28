"""free search funnel

Revision ID: 5b1f0e7c2a94
Revises: 829548981863
Create Date: 2026-10-28 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5b1f0e7c2a94'
down_revision: Union[str, Sequence[str], None] = '829548981863'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('organizations', sa.Column('signup_offer', sa.String(length=30), server_default='', nullable=False))
    op.add_column('organizations', sa.Column('offer_seen_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('organizations', sa.Column('checkout_started_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('organizations', 'checkout_started_at')
    op.drop_column('organizations', 'offer_seen_at')
    op.drop_column('organizations', 'signup_offer')
