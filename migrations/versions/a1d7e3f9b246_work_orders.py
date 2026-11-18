"""work orders, and dimensions on items

`work_orders` and `work_order_lines`: numbered documents releasing a set of a
project's items to production, each line holding its own copy of the text it
printed. `items.dimensions`: free text, so a work order has something to print
without it being typed every time.

Revision ID: a1d7e3f9b246
Revises: f8b3d5a7c920
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'a1d7e3f9b246'
down_revision = 'f8b3d5a7c920'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('items') as batch:
        batch.add_column(sa.Column('dimensions', sa.String(length=255), nullable=True))

    op.create_table(
        'work_orders',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('title', sa.String(length=160), nullable=False),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('show_picture', sa.Boolean(), nullable=False),
        sa.Column('show_description', sa.Boolean(), nullable=False),
        sa.Column('show_dimensions', sa.Boolean(), nullable=False),
        sa.Column('show_location', sa.Boolean(), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('issued_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['issued_by_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_work_orders_number', 'work_orders', ['number'], unique=True)
    op.create_index('ix_work_orders_project_id', 'work_orders', ['project_id'], unique=False)

    op.create_table(
        'work_order_lines',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('work_order_id', sa.Uuid(), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Uuid(), nullable=True),
        sa.Column('image_id', sa.Uuid(), nullable=True),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('dimensions', sa.String(length=255), nullable=False),
        sa.Column('qty', sa.Integer(), nullable=False),
        sa.Column('area', sa.String(length=120), nullable=False),
        sa.Column('phase', sa.String(length=120), nullable=False),
        sa.Column('drawing_revision', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['image_id'], ['item_images.id']),
        sa.ForeignKeyConstraint(['item_id'], ['items.id']),
        sa.ForeignKeyConstraint(['work_order_id'], ['work_orders.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_work_order_lines_work_order_id', 'work_order_lines', ['work_order_id'])
    op.create_index('ix_work_order_lines_item_id', 'work_order_lines', ['item_id'])
    op.create_index('ix_work_order_lines_image_id', 'work_order_lines', ['image_id'])


def downgrade() -> None:
    op.drop_index('ix_work_order_lines_image_id', table_name='work_order_lines')
    op.drop_index('ix_work_order_lines_item_id', table_name='work_order_lines')
    op.drop_index('ix_work_order_lines_work_order_id', table_name='work_order_lines')
    op.drop_table('work_order_lines')
    op.drop_index('ix_work_orders_project_id', table_name='work_orders')
    op.drop_index('ix_work_orders_number', table_name='work_orders')
    op.drop_table('work_orders')
    with op.batch_alter_table('items') as batch:
        batch.drop_column('dimensions')
