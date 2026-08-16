"""events.submitted_by_user_id: who actually posted the row

Revision ID: a4c7d1e2b9f0
Revises: 9f8e2c11ab30
Create Date: 2026-08-16

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'a4c7d1e2b9f0'
down_revision = '9f8e2c11ab30'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'events',
        sa.Column('submitted_by_user_id', sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        'fk_events_submitted_by_user_id_users',
        'events',
        'users',
        ['submitted_by_user_id'],
        ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_events_submitted_by_user_id_users', 'events', type_='foreignkey')
    op.drop_column('events', 'submitted_by_user_id')
