"""Item setup: the stage sequence, where the quantities start, and revisions.

An item's route is its own. It is chosen stage by stage when the item is
created; the office screen can fill that picker from a sibling item in the same
project, which is how the second wardrobe gets the wardrobe sequence without
anyone maintaining a template. Nothing is shared afterwards -- that copy happens
before the item exists, and editing one item's stages can never reach another's.

Creating an item is the handoff to the floor. There is no separate release:
the item is loggable from the moment it exists, and its stages are rewritable
only until the floor has actually logged against it, because from then on the
steps are what the stored events refer to.

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
    Event,
    EventType,
    Item,
    ItemStep,
    Stage,
    User,
)


class ItemSetupError(Exception):
    pass


def references_a_step():
    """An event that names one of its item's steps, as where its units went or
    as where they came from. That is what freezes a sequence."""
    return sa.or_(Event.item_step_id.is_not(None), Event.from_item_step_id.is_not(None))


def steps_are_frozen(db: Session, item: Item) -> bool:
    """True once any stored event points at one of this item's steps.

    Item-level events (the entered-production marker, a revision bump) carry no
    step, so an item can still be corrected right after it is created.
    """
    return (
        db.scalar(
            sa.select(sa.func.count())
            .select_from(Event)
            .where(Event.item_id == item.id, references_a_step())
        )
        > 0
    )


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


def assign_steps(
    db: Session,
    item: Item,
    stage_ids: list[uuid.UUID],
    created_at: datetime | None = None,
) -> list[ItemStep]:
    """Give the item its production sequence, replacing whatever it had.

    Sequence numbers are multiples of 10 so a later correction can slot a stage
    between two existing ones without renumbering the rest.
    """
    if not stage_ids:
        raise ItemSetupError("an item needs at least one stage")

    stages = {s.id: s for s in db.scalars(sa.select(Stage))}
    for stage_id in stage_ids:
        if stage_id not in stages:
            raise ItemSetupError(f"unknown stage {stage_id}")
        if not stages[stage_id].is_active:
            raise ItemSetupError(f"stage '{stages[stage_id].name}' is retired")

    # `is_terminal` marks a stage that closes a sequence (packing): nothing may
    # follow it. It no longer finishes units by itself -- that is a finish
    # entry, into Done -- except on legacy rows, where "completed" at a terminal
    # stage replays as Done.
    for stage_id in stage_ids[:-1]:
        if stages[stage_id].is_terminal:
            raise ItemSetupError(
                f"stage '{stages[stage_id].name}' closes a sequence and must be the last stage"
            )

    if steps_are_frozen(db, item):
        raise ItemSetupError(
            f"{item.code} already has entries logged against its stages; "
            "correct those first, or create a new item"
        )

    for existing in db.scalars(sa.select(ItemStep).where(ItemStep.item_id == item.id)):
        db.delete(existing)
    db.flush()

    moment = created_at or utcnow()
    steps = []
    for position, stage_id in enumerate(stage_ids, start=1):
        step = ItemStep(
            item_id=item.id, seq=position * 10, stage_id=stage_id, created_at=moment
        )
        db.add(step)
        steps.append(step)
    db.flush()
    return steps


def place_initial_quantities(
    db: Session,
    item: Item,
    steps: list[ItemStep],
    quantities: dict[int, int],
    user: User,
    moment: datetime | None = None,
) -> None:
    """Onboard an item that is already mid-production when it is entered.

    Each placement is an ordinary arrival at its stage, so the ledger needs no
    special case. Only for an item without a sample: a part-built batch is past
    its sample by definition, and with one the batch would not exist yet for
    the placements to draw from. Deepest step first, each a millisecond apart: the replay pulls from the
    *nearest* upstream position, and only this ordering guarantees every
    placement draws from the unstarted pool rather than from a shallower
    placement that happened to land first.
    """
    if quantities and item.sample_qty:
        raise ItemSetupError(
            "a part-built batch has no sample to run; set the sample quantity to 0"
        )
    step_by_seq = {step.seq: step for step in steps}
    for seq, qty in quantities.items():
        if seq not in step_by_seq:
            raise ItemSetupError(f"step {seq} is not one of this item's stages")
        if qty <= 0:
            raise ItemSetupError(f"quantity for step {seq} must be positive")
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
    for offset, seq in enumerate(sorted(quantities, reverse=True), start=1):
        db.add(
            Event(
                id=uuid.uuid4(),
                item_id=item.id,
                item_step_id=step_by_seq[seq].id,
                event_type_id=move_type.id,
                qty=quantities[seq],
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
        item_step_id=None,
        event_type_id=bump_type.id,
        state_id=None,
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
