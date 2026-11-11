"""may be closed

Revision ID: e5b2c8f1a634
Revises: f1c7d2a9b385
Create Date: 2026-11-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b2c8f1a634'
down_revision: Union[str, Sequence[str], None] = 'f1c7d2a9b385'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DATES = ('closed_flagged_at', 'dead_since', 'closed_checked_at', 'open_confirmed_at', 'closed_reported_at')


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('leads', sa.Column('closed_reason', sa.String(length=300), server_default='', nullable=False))
    for name in DATES:
        op.add_column('leads', sa.Column(name, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for name in DATES:
        op.drop_column('leads', name)
    op.drop_column('leads', 'closed_reason')
