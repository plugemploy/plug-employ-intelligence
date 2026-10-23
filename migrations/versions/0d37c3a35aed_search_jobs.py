"""search jobs

Revision ID: 0d37c3a35aed
Revises: 4fa5b77aeb58
Create Date: 2026-10-22 20:07:07.899570

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0d37c3a35aed'
down_revision: Union[str, Sequence[str], None] = '4fa5b77aeb58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('search_jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='running', nullable=False),
    sa.Column('step', sa.String(length=50), server_default='', nullable=False),
    sa.Column('zips', sa.Text(), server_default='[]', nullable=False),
    sa.Column('industries', sa.Text(), server_default='[]', nullable=False),
    sa.Column('results', sa.Text(), server_default='[]', nullable=False),
    sa.Column('error', sa.Text(), server_default='', nullable=False),
    sa.Column('seen', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_search_jobs_account_id'), 'search_jobs', ['account_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_search_jobs_account_id'), table_name='search_jobs')
    op.drop_table('search_jobs')
