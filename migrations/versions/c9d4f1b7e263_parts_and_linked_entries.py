"""parts, linked entries, and batches that share a sample

`items.parent_item_id` / `items.per_unit`: an item can be split into parts,
each tracked on its own and brought back together at assembly.
`items.sample_item_id`: a batch split off into another phase before its sample
was done is released by that item's sample.
`events.link_id`, `from_link`, `to_link`: the entries of a split, an assembly
or a move into another phase, one row per item, grouped.

Nothing existing changes meaning: every current item has no parts, no shared
sample, and every current entry is unlinked.

Revision ID: c9d4f1b7e263
Revises: b5c2e8a4d137
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'c9d4f1b7e263'
down_revision = 'b5c2e8a4d137'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('items') as batch:
        batch.add_column(sa.Column('parent_item_id', sa.Uuid(), nullable=True))
        batch.add_column(sa.Column('per_unit', sa.Integer(), nullable=False, server_default='1'))
        batch.add_column(sa.Column('sample_item_id', sa.Uuid(), nullable=True))
        batch.create_foreign_key('fk_items_parent_item_id', 'items', ['parent_item_id'], ['id'])
        batch.create_foreign_key('fk_items_sample_item_id', 'items', ['sample_item_id'], ['id'])
        batch.create_index('ix_items_parent_item_id', ['parent_item_id'])
    with op.batch_alter_table('events') as batch:
        batch.add_column(sa.Column('link_id', sa.Uuid(), nullable=True))
        batch.add_column(sa.Column('from_link', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column('to_link', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_index('ix_events_link_id', ['link_id'])


def downgrade() -> None:
    with op.batch_alter_table('events') as batch:
        batch.drop_index('ix_events_link_id')
        batch.drop_column('to_link')
        batch.drop_column('from_link')
        batch.drop_column('link_id')
    with op.batch_alter_table('items') as batch:
        batch.drop_index('ix_items_parent_item_id')
        batch.drop_constraint('fk_items_sample_item_id', type_='foreignkey')
        batch.drop_constraint('fk_items_parent_item_id', type_='foreignkey')
        batch.drop_column('sample_item_id')
        batch.drop_column('per_unit')
        batch.drop_column('parent_item_id')
