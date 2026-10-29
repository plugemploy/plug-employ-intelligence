"""terms agreement

Revision ID: b7d1e4f2a905
Revises: 9a4e6c2d1b83
Create Date: 2026-10-28 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d1e4f2a905'
down_revision: Union[str, Sequence[str], None] = '9a4e6c2d1b83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('terms_agreed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('terms_version', sa.String(length=20), server_default='', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'terms_version')
    op.drop_column('users', 'terms_agreed_at')
