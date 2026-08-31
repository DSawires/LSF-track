"""Drop route templates and the release step.

An item's stage sequence is now its own, chosen when the item is created, and
creating an item IS the handoff to the floor -- so `route_templates`,
`route_template_steps`, `items.route_template_id` and the three `released_*`
columns all go.

Nothing derivable is lost. `item_steps` is untouched: it already holds each
released item's own sequence, which is exactly what the new model wants, so
items in production keep the stages their events point at. Items that were
sitting unreleased have no steps and no events; they come out the other side as
ordinary items with an empty sequence, which the office screen prompts to fill
in before anything can be logged against them.

Downgrade restores the columns and the (empty) tables; it cannot invent the
templates that were dropped, and every item comes back looking released, dated
from when it was created. That is the honest inverse -- under the old model an
item with steps was a released item.

Revision ID: f2b6d90a4e71
Revises: e1a5c73b6d92
"""

from alembic import op
import sqlalchemy as sa

revision = 'f2b6d90a4e71'
down_revision = 'e1a5c73b6d92'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL drops a column's foreign key with the column, so the two
    # unnamed constraints from the initial schema need no separate handling.
    op.drop_column('items', 'route_template_id')
    op.drop_column('items', 'released_by_user_id')
    op.drop_column('items', 'released_at')
    op.drop_column('items', 'released_revision')

    op.drop_index('ix_route_template_steps_route_template_id', table_name='route_template_steps')
    op.drop_table('route_template_steps')
    op.drop_index('ix_route_templates_code', table_name='route_templates')
    op.drop_table('route_templates')


def downgrade() -> None:
    op.create_table(
        'route_templates',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('code', sa.String(length=48), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('is_published', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', 'version', name='uq_route_code_version'),
    )
    op.create_index('ix_route_templates_code', 'route_templates', ['code'], unique=False)
    op.create_table(
        'route_template_steps',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('route_template_id', sa.Uuid(), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('stage_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['route_template_id'], ['route_templates.id'], ),
        sa.ForeignKeyConstraint(['stage_id'], ['stages.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('route_template_id', 'seq', name='uq_route_step_seq'),
    )
    op.create_index(
        'ix_route_template_steps_route_template_id',
        'route_template_steps', ['route_template_id'], unique=False,
    )

    op.add_column('items', sa.Column('route_template_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'items_route_template_id_fkey', 'items', 'route_templates',
        ['route_template_id'], ['id'],
    )
    op.add_column('items', sa.Column('released_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('items', sa.Column('released_by_user_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'items_released_by_user_id_fkey', 'items', 'users', ['released_by_user_id'], ['id'],
    )
    op.add_column('items', sa.Column('released_revision', sa.String(length=32), nullable=True))

    # An item with steps was, under the old model, a released item.
    op.execute(
        """
        UPDATE items SET released_at = created_at, released_revision = drawing_revision
        WHERE id IN (SELECT DISTINCT item_id FROM item_steps)
        """
    )
