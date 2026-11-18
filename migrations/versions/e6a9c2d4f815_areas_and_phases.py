"""areas and phases; an item is project + code + area + phase

Two lists the office edits, and two optional references on `items`. The same
item code can now be several batches in one project -- one per area and phase
-- so `items.code` stops being unique on its own. Identity is checked in the
application (app/services/items.py): a NULL area or phase is part of it, and a
unique index treats NULLs as never equal.

Existing items get no area and no phase, and are exactly what they were.

Revision ID: e6a9c2d4f815
Revises: d3b8f61a2c47
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'e6a9c2d4f815'
down_revision = 'd3b8f61a2c47'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ('areas', 'phases'):
        op.create_table(
            table,
            sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('sort_order', sa.Integer(), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(f'ix_{table}_name', table, ['name'], unique=True)

    # batch_alter_table: a no-op wrapper on PostgreSQL, the copy-and-move
    # strategy SQLite needs for adding a foreign key.
    with op.batch_alter_table('items') as batch:
        batch.add_column(sa.Column('area_id', sa.Uuid(), nullable=True))
        batch.add_column(sa.Column('phase_id', sa.Uuid(), nullable=True))
        batch.create_foreign_key('fk_items_area_id_areas', 'areas', ['area_id'], ['id'])
        batch.create_foreign_key('fk_items_phase_id_phases', 'phases', ['phase_id'], ['id'])
        batch.create_index('ix_items_area_id', ['area_id'])
        batch.create_index('ix_items_phase_id', ['phase_id'])
        # The code alone no longer names one item.
        batch.drop_index('ix_items_code')
        batch.create_index('ix_items_code', ['code'], unique=False)


def downgrade() -> None:
    # Only possible while no code is used twice; the unique index refuses otherwise.
    with op.batch_alter_table('items') as batch:
        batch.drop_index('ix_items_code')
        batch.create_index('ix_items_code', ['code'], unique=True)
        batch.drop_index('ix_items_phase_id')
        batch.drop_index('ix_items_area_id')
        batch.drop_constraint('fk_items_phase_id_phases', type_='foreignkey')
        batch.drop_constraint('fk_items_area_id_areas', type_='foreignkey')
        batch.drop_column('phase_id')
        batch.drop_column('area_id')
    for table in ('phases', 'areas'):
        op.drop_index(f'ix_{table}_name', table_name=table)
        op.drop_table(table)
