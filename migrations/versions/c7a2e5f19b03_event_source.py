"""events.from_item_step_id / from_done: where the units came from

A rework entry used to take its units from the nearest stage downstream of its
target, whichever stage actually sent them back. An entry can now name its
source: one of the item's steps, or Done. Both are empty on every existing row,
which keeps replaying exactly as before.

Revision ID: c7a2e5f19b03
Revises: b4e1c9d27a58
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'c7a2e5f19b03'
down_revision = 'b4e1c9d27a58'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table: a no-op wrapper on PostgreSQL, the copy-and-move
    # strategy SQLite needs for adding a foreign key.
    with op.batch_alter_table('events') as batch:
        batch.add_column(sa.Column('from_item_step_id', sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column('from_done', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.create_foreign_key(
            'fk_events_from_item_step_id_item_steps',
            'item_steps',
            ['from_item_step_id'],
            ['id'],
        )


def downgrade() -> None:
    with op.batch_alter_table('events') as batch:
        batch.drop_constraint('fk_events_from_item_step_id_item_steps', type_='foreignkey')
        batch.drop_column('from_done')
        batch.drop_column('from_item_step_id')
