"""user activity

Revision ID: 9a4e6c2d1b83
Revises: 3c8d2a1f6b70
Create Date: 2026-10-28 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a4e6c2d1b83'
down_revision: Union[str, Sequence[str], None] = '3c8d2a1f6b70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('active_seconds', sa.Integer(), server_default='0', nullable=False))
    op.add_column('users', sa.Column('visits', sa.Integer(), server_default='0', nullable=False))
    op.add_column('users', sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'last_seen_at')
    op.drop_column('users', 'visits')
    op.drop_column('users', 'active_seconds')
