"""feedback

Revision ID: 4fa5b77aeb58
Revises: 3223d9ac8369
Create Date: 2026-10-22 16:19:38.442980

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4fa5b77aeb58'
down_revision: Union[str, Sequence[str], None] = '3223d9ac8369'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('feedback',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('page', sa.String(length=300), server_default='', nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_feedback_account_id'), 'feedback', ['account_id'], unique=False)
    op.create_index(op.f('ix_feedback_created_at'), 'feedback', ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_feedback_created_at'), table_name='feedback')
    op.drop_index(op.f('ix_feedback_account_id'), table_name='feedback')
    op.drop_table('feedback')
