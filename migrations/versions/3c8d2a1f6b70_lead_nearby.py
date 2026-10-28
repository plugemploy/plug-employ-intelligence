"""lead nearby

Revision ID: 3c8d2a1f6b70
Revises: 5b1f0e7c2a94
Create Date: 2026-10-28 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3c8d2a1f6b70'
down_revision: Union[str, Sequence[str], None] = '5b1f0e7c2a94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('leads', sa.Column('nearby', sa.String(length=120), server_default='', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('leads', 'nearby')
