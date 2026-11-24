"""Arabic names for stages and stations

`stages.name_ar`, `stations.name_ar`: the name a phone set to Arabic shows.
Optional; NULL keeps showing `name`, which is what every existing row does.

Revision ID: a7e2c4f9d158
Revises: c9d4f1b7e263
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'a7e2c4f9d158'
down_revision = 'c9d4f1b7e263'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('stages') as batch:
        batch.add_column(sa.Column('name_ar', sa.String(length=120), nullable=True))
    with op.batch_alter_table('stations') as batch:
        batch.add_column(sa.Column('name_ar', sa.String(length=120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('stations') as batch:
        batch.drop_column('name_ar')
    with op.batch_alter_table('stages') as batch:
        batch.drop_column('name_ar')
