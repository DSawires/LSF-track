"""users.last_login_at and the status_banners log.

Two halves of the same admin status page. `last_login_at` is the one thing on
that page the event log cannot answer: signing in is not an event about an
item, so it is a column on the user, stamped at login.

`status_banners` is insert-only for the same reason `events` is -- the banner
is a statement someone made at a time, and replacing it in place would throw
away who raised the red and when. The latest row is the live banner; clearing
it is a new `neutral` row.

Revision ID: e1a5c73b6d92
Revises: d7e9b2c4f1a3
Create Date: 2026-08-17

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'e1a5c73b6d92'
down_revision = 'd7e9b2c4f1a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL for every existing account: "never seen signing in since this
    # shipped" is the truth, and a backfilled guess would read as a fact.
    op.add_column('users', sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        'status_banners',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('color', sa.String(length=16), nullable=False),
        sa.Column('message', sa.String(length=200), nullable=False),
        sa.Column('set_by_user_id', sa.Uuid(), nullable=False),
        sa.Column('set_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['set_by_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    # The only query this table has: newest row first.
    op.create_index('ix_status_banners_set_at', 'status_banners', ['set_at'])


def downgrade() -> None:
    op.drop_index('ix_status_banners_set_at', table_name='status_banners')
    op.drop_table('status_banners')
    op.drop_column('users', 'last_login_at')
