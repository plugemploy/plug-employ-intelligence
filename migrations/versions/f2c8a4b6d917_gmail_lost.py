"""gmail_lost: address whose Gmail connection Google stopped accepting

Revision ID: f2c8a4b6d917
Revises: e7b3d5f8a206
Create Date: 2026-11-11 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2c8a4b6d917'
down_revision: Union[str, Sequence[str], None] = 'e7b3d5f8a206'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('accounts', sa.Column('gmail_lost', sa.String(length=300), server_default='', nullable=False))


def downgrade() -> None:
    op.drop_column('accounts', 'gmail_lost')
