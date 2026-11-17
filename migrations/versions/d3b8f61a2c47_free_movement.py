"""events name stages directly: free movement, no per-item sequence

Items no longer carry a stage sequence. An entry says which stage the units are
at now (`stage_id`) and where they came from (`from_stage_id`, or
`from_unstarted`, or the existing `from_done`), so the movement itself is on
the row.

Nothing is dropped or rewritten. `item_steps`, `events.item_step_id` and
`events.from_item_step_id` stay, because entries already in the log point at
them and replay through them.

Revision ID: d3b8f61a2c47
Revises: c7a2e5f19b03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'd3b8f61a2c47'
down_revision = 'c7a2e5f19b03'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table: a no-op wrapper on PostgreSQL, the copy-and-move
    # strategy SQLite needs for adding a foreign key.
    with op.batch_alter_table('events') as batch:
        batch.add_column(sa.Column('stage_id', sa.Uuid(), nullable=True))
        batch.add_column(sa.Column('from_stage_id', sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column('from_unstarted', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.create_foreign_key(
            'fk_events_stage_id_stages', 'stages', ['stage_id'], ['id']
        )
        batch.create_foreign_key(
            'fk_events_from_stage_id_stages', 'stages', ['from_stage_id'], ['id']
        )
        batch.create_index('ix_events_stage_id', ['stage_id'])


def downgrade() -> None:
    with op.batch_alter_table('events') as batch:
        batch.drop_index('ix_events_stage_id')
        batch.drop_constraint('fk_events_from_stage_id_stages', type_='foreignkey')
        batch.drop_constraint('fk_events_stage_id_stages', type_='foreignkey')
        batch.drop_column('from_unstarted')
        batch.drop_column('from_stage_id')
        batch.drop_column('stage_id')
