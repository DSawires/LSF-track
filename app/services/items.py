"""Item setup: where the quantities start, archiving, and revisions.

An item has no stages of its own. It is created with a code, a batch size and
a drawing revision, and from that moment it can be logged at any stage in the
factory, in any order: the log is the record of where its units have been.

Creating an item is the handoff to the floor. There is no separate release:
the item is loggable from the moment it exists.

Every item starts as a sample (`sample_qty`, 1 unless the office says
otherwise). The production batch is released by the ledger when the sample
reaches Done -- derived from the log, so nothing here writes it.

Nothing is written to the log to mark that handoff. `items.created_at` is when
production started counting -- the ledger reads it as the aging origin -- and a
marker event on every single item would say nothing the item row does not, while
making every item look like it had history worth keeping.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    Area,
    Event,
    EventType,
    Item,
    Phase,
    Project,
    Stage,
    User,
)


class ItemSetupError(Exception):
    pass


def identity_taken(
    db: Session,
    project_id: uuid.UUID,
    code: str,
    area_id: uuid.UUID | None,
    phase_id: uuid.UUID | None,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    """Is there already an item with this project, code, area and phase?

    Those four together are what an item *is*: the same code may be several
    batches in one project, one per area and phase. "No area" and "no phase"
    are values like any other, which is why this is a query and not a unique
    index -- an index would let two items with the same code and no area sit
    side by side, because it never considers two NULLs equal. Archived items
    count: bringing one back must not collide with its replacement.
    """
    query = sa.select(Item.id).where(
        Item.project_id == project_id,
        Item.code == code,
        Item.area_id == area_id if area_id is not None else Item.area_id.is_(None),
        Item.phase_id == phase_id if phase_id is not None else Item.phase_id.is_(None),
    )
    if exclude_id is not None:
        query = query.where(Item.id != exclude_id)
    return db.scalars(query.limit(1)).first() is not None


def lock_project(db: Session, project_id: uuid.UUID) -> Project | None:
    """The project row, locked until the request commits.

    Anything that creates or re-labels an item takes this first, so two
    requests cannot both check `identity_taken`, both find the way clear and
    both insert. (SQLite has no row locks and ignores it; it serialises
    writers on its own.)
    """
    return db.get(Project, project_id, with_for_update=True)


def identity_label(code: str, area: Area | None, phase: Phase | None) -> str:
    """How an item is named to a person: "LF-01 (Guest rooms, Phase 2)"."""
    parts = [part.name for part in (area, phase) if part is not None]
    return f"{code} ({', '.join(parts)})" if parts else code


def archive_item(db: Session, item: Item, user: User) -> None:
    """Take an item off every list and report, keeping its history.

    Archiving removes the item's quantities from WIP, so it is written to the
    log (who, when) rather than being a silent flag flip. Reversible: see the
    `is_active` branch of PATCH /api/items/{id}.
    """
    item.is_active = False
    archive_type = _event_type(db, "is_archive")
    if archive_type is not None:
        db.add(
            Event(
                id=uuid.uuid4(),
                item_id=item.id,
                event_type_id=archive_type.id,
                qty=0,
                occurred_at=utcnow(),
                received_at=utcnow(),
                user_id=user.id,
                submitted_by_user_id=user.id,
                note="item archived",
            )
        )
    db.flush()


def place_initial_quantities(
    db: Session,
    item: Item,
    quantities: dict[uuid.UUID, int],
    user: User,
    moment: datetime | None = None,
) -> None:
    """Onboard an item that is already mid-production when it is entered.

    `quantities` is how many units are at each stage, keyed by stage id. Each
    placement is an ordinary move out of "not started", so the ledger needs no
    special case. Only for an item without a sample: a part-built batch is past
    its sample by definition, and with one the batch would not exist yet for
    the placements to draw from.
    """
    if quantities and item.sample_qty:
        raise ItemSetupError(
            "a part-built batch has no sample to run; set the sample quantity to 0"
        )
    stages = {s.id: s for s in db.scalars(sa.select(Stage))}
    for stage_id, qty in quantities.items():
        if stage_id not in stages:
            raise ItemSetupError(f"unknown stage {stage_id}")
        if qty <= 0:
            raise ItemSetupError(f"quantity at '{stages[stage_id].name}' must be positive")
    total = sum(quantities.values())
    if total > item.total_qty:
        raise ItemSetupError(
            f"distributed {total} across stages but the batch is only {item.total_qty}"
        )

    move_type = db.scalars(
        sa.select(EventType).where(
            EventType.moves_quantity.is_(True),
            EventType.is_rework.is_(False),
            EventType.is_correction.is_(False),
            EventType.is_finish.is_(False),
            EventType.is_active.is_(True),
        ).order_by(EventType.sort_order).limit(1)
    ).first()
    if move_type is None:
        raise ItemSetupError("no movement event type is seeded")

    at = moment or utcnow()
    ordered = sorted(quantities, key=lambda sid: (stages[sid].sort_order, stages[sid].code))
    for offset, stage_id in enumerate(ordered, start=1):
        db.add(
            Event(
                id=uuid.uuid4(),
                item_id=item.id,
                stage_id=stage_id,
                from_unstarted=True,
                event_type_id=move_type.id,
                qty=quantities[stage_id],
                # A millisecond apart, only so the entries list in a stable order.
                occurred_at=at + timedelta(milliseconds=offset),
                received_at=utcnow(),
                user_id=user.id,
                submitted_by_user_id=user.id,
                note="starting position when the item was entered",
            )
        )
    db.flush()


def bump_revision(db: Session, item: Item, revision: str, user: User) -> Event | None:
    """Issue a drawing revision after the item is in production.

    The point of recording it is that anything already in flight was built to the
    old drawing. The quantity affected is derived from the log, not stored.
    """
    if revision == item.drawing_revision:
        raise ItemSetupError(f"{item.code} is already at revision {revision}")

    previous = item.drawing_revision
    item.drawing_revision = revision

    bump_type = _event_type(db, "is_revision_bump")
    if bump_type is None:
        db.flush()
        return None
    event = Event(
        id=uuid.uuid4(),
        item_id=item.id,
        event_type_id=bump_type.id,
        qty=0,
        occurred_at=utcnow(),
        received_at=utcnow(),
        user_id=user.id,
        submitted_by_user_id=user.id,
        note=f"drawing revision {previous} -> {revision}",
    )
    db.add(event)
    db.flush()
    return event


def _event_type(db: Session, flag: str) -> EventType | None:
    """Find the event type carrying a behaviour flag, rather than naming a code."""
    column = getattr(EventType, flag, None)
    if column is None:
        return None
    return db.scalars(
        sa.select(EventType).where(column.is_(True), EventType.is_active.is_(True)).limit(1)
    ).first()
