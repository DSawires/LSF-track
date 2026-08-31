"""Drop items.target_release_date.

The column was the office's intended release date, from when releasing an item
was a distinct act. Nothing releases any more -- creating an item is the handoff
-- and the field was never read by a report, a query or the ledger: it was shown
in two forms and stored. It goes rather than lingering as a date nobody sets and
nothing derives from.

Downgrade restores the column as NULL for every row. The dates themselves are
not recoverable from the log, because they never were in it.

Revision ID: a3d81f5c2e64
Revises: f2b6d90a4e71
"""

from alembic import op
import sqlalchemy as sa

revision = 'a3d81f5c2e64'
down_revision = 'f2b6d90a4e71'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('items', 'target_release_date')


def downgrade() -> None:
    op.add_column('items', sa.Column('target_release_date', sa.Date(), nullable=True))
