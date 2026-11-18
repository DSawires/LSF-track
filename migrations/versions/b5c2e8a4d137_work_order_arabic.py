"""work orders in Arabic

A work order can be printed in English, Arabic or both. The Arabic wording is
stored beside the English: `title_ar` and `notes_ar` on the work order,
`description_ar` on each line. `language` says which the PDF prints.

Existing work orders become English ones with no Arabic text, which is what
they were.

Revision ID: b5c2e8a4d137
Revises: a1d7e3f9b246
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'b5c2e8a4d137'
down_revision = 'a1d7e3f9b246'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('work_orders') as batch:
        batch.add_column(sa.Column('language', sa.String(length=8), nullable=False, server_default='en'))
        batch.add_column(sa.Column('title_ar', sa.String(length=160), nullable=False, server_default=''))
        batch.add_column(sa.Column('notes_ar', sa.Text(), nullable=False, server_default=''))
    with op.batch_alter_table('work_order_lines') as batch:
        batch.add_column(sa.Column('description_ar', sa.Text(), nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('work_order_lines') as batch:
        batch.drop_column('description_ar')
    with op.batch_alter_table('work_orders') as batch:
        batch.drop_column('notes_ar')
        batch.drop_column('title_ar')
        batch.drop_column('language')
