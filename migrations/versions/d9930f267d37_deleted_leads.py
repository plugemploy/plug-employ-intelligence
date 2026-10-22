"""deleted leads

Revision ID: d9930f267d37
Revises: 1a1549704448
Create Date: 2026-10-22 12:29:49.538076

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9930f267d37'
down_revision: Union[str, Sequence[str], None] = '1a1549704448'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('leads', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_leads_deleted_at'), 'leads', ['deleted_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_leads_deleted_at'), table_name='leads')
    op.drop_column('leads', 'deleted_at')
