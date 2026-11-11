"""closed removal: down_since

Revision ID: b8d4f2a6c913
Revises: e5b2c8f1a634
Create Date: 2026-11-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d4f2a6c913'
down_revision: Union[str, Sequence[str], None] = 'e5b2c8f1a634'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('leads', sa.Column('down_since', sa.DateTime(timezone=True), nullable=True))
    # Leads whose website failed before: looked at again on the next daily check (the monthly sweep's group).
    op.execute("UPDATE leads SET closed_checked_at = NULL WHERE website_status = 'error' AND closed_reason = ''")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('leads', 'down_since')
