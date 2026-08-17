"""Drop event_types.counts_toward_completion.

The flag was never read: completion counting is structural (FIFO lots at
final positions cannot double-count a re-completion), so the column only
advertised behaviour that nothing implemented. An advertised-but-dead flag
is worse than none -- an operator adding an event type would reasonably
expect it to do something.

Revision ID: b8e3f6a0c2d4
Revises: a4c7d1e2b9f0
Create Date: 2026-08-16

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'b8e3f6a0c2d4'
down_revision = 'a4c7d1e2b9f0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('event_types', 'counts_toward_completion')


def downgrade() -> None:
    op.add_column(
        'event_types',
        sa.Column(
            'counts_toward_completion',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
