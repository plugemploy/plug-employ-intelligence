"""trial started at

Revision ID: d4a9b3e7f218
Revises: c3f8a2d6e417
Create Date: 2026-10-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a9b3e7f218'
down_revision: Union[str, Sequence[str], None] = 'c3f8a2d6e417'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('organizations', sa.Column('trial_started_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('organizations', 'trial_started_at')
