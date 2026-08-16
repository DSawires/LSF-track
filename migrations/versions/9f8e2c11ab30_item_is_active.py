"""items.is_active for archiving

Revision ID: 9f8e2c11ab30
Revises: 7c41aa09be22
Create Date: 2026-08-16

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '9f8e2c11ab30'
down_revision = '7c41aa09be22'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'items',
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column('items', 'is_active')
