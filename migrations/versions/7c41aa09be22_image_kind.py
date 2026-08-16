"""image kind: snag or icon

Revision ID: 7c41aa09be22
Revises: 0137edf5c1c0
Create Date: 2026-08-16

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '7c41aa09be22'
down_revision = '0137edf5c1c0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'item_images',
        sa.Column('kind', sa.String(length=16), nullable=False, server_default='snag'),
    )


def downgrade() -> None:
    op.drop_column('item_images', 'kind')
