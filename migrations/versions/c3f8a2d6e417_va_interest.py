"""va interest

Revision ID: c3f8a2d6e417
Revises: b7d1e4f2a905
Create Date: 2026-10-28 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f8a2d6e417'
down_revision: Union[str, Sequence[str], None] = 'b7d1e4f2a905'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('organizations', sa.Column('va_interest_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('organizations', 'va_interest_at')
