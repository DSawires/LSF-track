"""event_states.is_initial and event_types.is_archive.

is_initial names the state new work enters a step in, so auto-queue and
queue-depth stop depending on sort order alone. The data update marks the
current lowest-sorted state, which is exactly what the code treated as the
queue until now -- no behaviour change for existing databases.

is_archive flags the item-level event the server writes when an admin
archives an item, so archiving leaves an audit trail in the append-only log
instead of a silent flag flip.

Revision ID: c5f2a8d413e7
Revises: b8e3f6a0c2d4
Create Date: 2026-08-16

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'c5f2a8d413e7'
down_revision = 'b8e3f6a0c2d4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'event_states',
        sa.Column('is_initial', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'event_types',
        sa.Column('is_archive', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Mark the state the code has treated as "the queue" until now.
    op.execute(
        "UPDATE event_states SET is_initial = "
        + ("1" if op.get_bind().dialect.name == "sqlite" else "TRUE")
        + " WHERE sort_order = (SELECT MIN(sort_order) FROM event_states es2)"
    )


def downgrade() -> None:
    op.drop_column('event_types', 'is_archive')
    op.drop_column('event_states', 'is_initial')
