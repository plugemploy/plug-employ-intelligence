"""full website addresses

Revision ID: 829548981863
Revises: 07c43469e763
Create Date: 2026-10-25 15:37:36.727763

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '829548981863'
down_revision: Union[str, Sequence[str], None] = '07c43469e763'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Websites saved without http(s):// (e.g. from a spreadsheet import) couldn't be read or linked: fix them and
    # let the website check read them.
    op.execute("""UPDATE leads SET website = 'https://' || trim(website),
                  website_checked_at = CASE WHEN website_status = 'no_site' THEN NULL ELSE website_checked_at END,
                  website_status = CASE WHEN website_status = 'no_site' THEN NULL ELSE website_status END
                  WHERE trim(website) <> '' AND trim(website) !~* '^https?://'
                    AND trim(website) LIKE '%.%' AND trim(website) NOT LIKE '% %' AND website NOT LIKE '%@%'""")


def downgrade() -> None:
    """Downgrade schema."""
    pass  # the old form wasn't usable; nothing to restore
