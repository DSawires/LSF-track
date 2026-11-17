"""Sample-first items, and a Done position instead of per-stage states.

`items.sample_qty`: how many units walk the sequence as a sample before the
production batch is released. Existing rows get 0 -- they are already in
production, and giving them a sample now would hide their batch behind a sample
nobody is going to build. New items default to 1 in the application.

`event_types.is_finish`: the flag on the event type that moves units off the
end of the sequence into Done. The row itself is seed data.

Nothing is dropped. `event_states` and `events.state_id` stay because the rows
already in the log reference them, and the log is append-only.

Revision ID: b4e1c9d27a58
Revises: a3d81f5c2e64
"""

from alembic import op
import sqlalchemy as sa

revision = 'b4e1c9d27a58'
down_revision = 'a3d81f5c2e64'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'items',
        sa.Column('sample_qty', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'event_types',
        sa.Column('is_finish', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('event_types', 'is_finish')
    op.drop_column('items', 'sample_qty')
