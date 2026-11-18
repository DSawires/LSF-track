"""areas and phases belong to a project

They were two lists shared by every project. Each project now has its own, so
`areas` and `phases` gain a `project_id`, and a name is unique within a project
instead of across the factory.

Rows that already exist are converted without losing anything a shared list
meant. A shared row was available to every project, so:

- a row that items are filed under goes to each project that uses it -- one
  project keeps the original row, every other gets its own copy and its items
  are pointed at that copy;
- a row nothing is filed under yet goes to every active project, since there
  is no telling which it was made for. Unwanted copies can be deleted in
  Office -> Areas & phases (a row with no items can be deleted outright).

Revision ID: f8b3d5a7c920
Revises: e6a9c2d4f815
"""
from __future__ import annotations

import uuid

from alembic import context, op
import sqlalchemy as sa


revision = 'f8b3d5a7c920'
down_revision = 'e6a9c2d4f815'
branch_labels = None
depends_on = None

_LISTS = (('areas', 'area_id'), ('phases', 'phase_id'))


def _tables(name: str, item_column: str):
    labels = sa.table(
        name,
        sa.column('id', sa.Uuid()),
        sa.column('project_id', sa.Uuid()),
        sa.column('name', sa.String()),
        sa.column('sort_order', sa.Integer()),
        sa.column('is_active', sa.Boolean()),
    )
    items = sa.table(
        'items',
        sa.column('id', sa.Uuid()),
        sa.column('project_id', sa.Uuid()),
        sa.column(item_column, sa.Uuid()),
    )
    return labels, items


def _convert(bind, name: str, item_column: str, active_projects: list) -> None:
    """Give every existing row a project, copying it where several need it."""
    labels, items = _tables(name, item_column)
    reference = items.c[item_column]
    used_by: dict = {}
    for label_id, project_id in bind.execute(
        sa.select(reference, items.c.project_id).where(reference.is_not(None)).distinct()
    ):
        used_by.setdefault(label_id, set()).add(project_id)

    rows = list(bind.execute(
        sa.select(labels.c.id, labels.c.name, labels.c.sort_order, labels.c.is_active)
    ))
    for label_id, label_name, sort_order, is_active in rows:
        homes = sorted(used_by.get(label_id, set()), key=str) or active_projects
        if not homes:
            # No project exists to own it, and nothing is filed under it.
            bind.execute(sa.delete(labels).where(labels.c.id == label_id))
            continue
        bind.execute(
            sa.update(labels).where(labels.c.id == label_id).values(project_id=homes[0])
        )
        for project_id in homes[1:]:
            copy_id = uuid.uuid4()
            bind.execute(sa.insert(labels).values(
                id=copy_id, project_id=project_id, name=label_name,
                sort_order=sort_order, is_active=is_active,
            ))
            bind.execute(
                sa.update(items)
                .where(reference == label_id, items.c.project_id == project_id)
                .values({item_column: copy_id})
            )



def upgrade() -> None:
    # `alembic upgrade --sql` has no database to read: it can print the DDL
    # but cannot convert rows. A real upgrade always runs online.
    offline = context.is_offline_mode()
    active_projects: list = []
    if not offline:
        projects = sa.table('projects', sa.column('id', sa.Uuid()), sa.column('is_active', sa.Boolean()))
        active_projects = sorted(
            (row[0] for row in op.get_bind().execute(
                sa.select(projects.c.id).where(projects.c.is_active.is_(True))
            )),
            key=str,
        )

    for name, item_column in _LISTS:
        with op.batch_alter_table(name) as batch:
            batch.add_column(sa.Column('project_id', sa.Uuid(), nullable=True))
            # A name stops being unique across the factory *before* any row is
            # copied: two projects are about to hold the same one.
            batch.drop_index(f'ix_{name}_name')

        if not offline:
            _convert(op.get_bind(), name, item_column, active_projects)

        with op.batch_alter_table(name) as batch:
            batch.alter_column('project_id', existing_type=sa.Uuid(), nullable=False)
            batch.create_foreign_key(f'fk_{name}_project_id_projects', 'projects', ['project_id'], ['id'])
            batch.create_index(f'ix_{name}_project_id', ['project_id'])
            batch.create_unique_constraint(f'uq_{name}_project_name', ['project_id', 'name'])


def downgrade() -> None:
    """Back to one shared list: rows with the same name are merged into one."""
    bind = op.get_bind()
    for name, item_column in _LISTS:
        labels, items = _tables(name, item_column)
        keep: dict[str, uuid.UUID] = {}
        for label_id, label_name in bind.execute(
            sa.select(labels.c.id, labels.c.name).order_by(labels.c.sort_order, labels.c.id)
        ):
            original = keep.setdefault(label_name, label_id)
            if original != label_id:
                bind.execute(
                    sa.update(items).where(items.c[item_column] == label_id)
                    .values({item_column: original})
                )
                bind.execute(sa.delete(labels).where(labels.c.id == label_id))
        with op.batch_alter_table(name) as batch:
            batch.drop_constraint(f'uq_{name}_project_name', type_='unique')
            batch.drop_index(f'ix_{name}_project_id')
            batch.drop_constraint(f'fk_{name}_project_id_projects', type_='foreignkey')
            batch.drop_column('project_id')
            batch.create_index(f'ix_{name}_name', ['name'], unique=True)
