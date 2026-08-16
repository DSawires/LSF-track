"""stages.max_days_in_state: the per-stage aging threshold.

"Sitting too long" is not one number: outsourced work rests for weeks by
design while paint should never sit three days. The threshold was hardcoded
as 3 in the client; it becomes an attribute on the stage row, per the
"stages are data" rule. NULL means no opinion and nothing is flagged.

Revision ID: d7e9b2c4f1a3
Revises: c5f2a8d413e7
Create Date: 2026-08-16

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'd7e9b2c4f1a3'
down_revision = 'c5f2a8d413e7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('stages', sa.Column('max_days_in_state', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('stages', 'max_days_in_state')
