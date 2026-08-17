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
    # batch_alter_table: a no-op wrapper on PostgreSQL, the copy-and-move
    # strategy SQLite needs for adding a foreign key.
    with op.batch_alter_table('events') as batch:
        batch.add_column(sa.Column('submitted_by_user_id', sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            'fk_events_submitted_by_user_id_users',
            'users',
            ['submitted_by_user_id'],
            ['id'],
        )


def downgrade() -> None:
    with op.batch_alter_table('events') as batch:
        batch.drop_constraint('fk_events_submitted_by_user_id_users', type_='foreignkey')
        batch.drop_column('submitted_by_user_id')
