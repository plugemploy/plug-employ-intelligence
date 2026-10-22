"""gmail consent

Revision ID: 3223d9ac8369
Revises: d9930f267d37
Create Date: 2026-10-22 15:10:05.005463

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3223d9ac8369'
down_revision: Union[str, Sequence[str], None] = 'd9930f267d37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('accounts', sa.Column('gmail_consent_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('accounts', 'gmail_consent_at')
