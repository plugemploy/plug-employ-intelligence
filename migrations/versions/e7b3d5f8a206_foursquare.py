"""foursquare: other_phone_source, fsq_checked_at

Revision ID: e7b3d5f8a206
Revises: d9a2c4e6f171
Create Date: 2026-11-11 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7b3d5f8a206'
down_revision: Union[str, Sequence[str], None] = 'd9a2c4e6f171'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('leads', sa.Column('other_phone_source', sa.String(length=20), server_default='', nullable=False))
    op.add_column('leads', sa.Column('fsq_checked_at', sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE leads SET other_phone_source = 'website' WHERE other_phone <> ''")


def downgrade() -> None:
    op.drop_column('leads', 'fsq_checked_at')
    op.drop_column('leads', 'other_phone_source')
