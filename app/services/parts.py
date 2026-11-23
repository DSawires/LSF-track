"""Parts, and batches split across phases.

Parts. An item can be split into the parts it is built from -- a bed into a
frame, a headboard and four legs -- each going through the factory on its own
and coming back together at assembly. A part is an item in its own right, with
its own code and log; `parent_item_id` says what it is part of and `per_unit`
how many go into one piece. Parts can have parts.

Splitting and assembling are entries in the log like any other, logged on the
floor and queued offline (static/js/app.js): a group of linked rows, one per
item, checked and stored together by services.events.record_group. This module
only manages which parts an item has.

The parts of an item may not change shape while pieces are out in parts: a
split of 10 beds made 10 frames, 10 headboards and 40 legs, and an assembly has
to take exactly those back. Assemble what is out first, then change the parts.

Phases. Units of a batch can be re-filed into another phase of the same
project -- 150 of the 216 bed bases for phase 1, 66 for phase 2. The other
phase's batch is the same code under that phase, made if it does not exist.

- Before the batch is released (its sample is not done), nothing is on the
  floor: the split moves planned quantity from one batch to the other, and the
  new batch shares the original's sample -- it is released the moment that
  sample is done, not before.
- After, it moves real units, place by place, as a linked pair of entries per
  place: out of this batch at that place, into the other at the same place.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Event, EventType, Item, Phase, Stage, User
from app.schemas import EventCreate
from app.services.derivation import load_vocabulary, replay_item
from app.services.events import EventRejected, parts_of, record_group
from app.services.items import identity_label, identity_taken, lock_project

UNSTARTED = "unstarted"


class PartsError(Exception):
    """Refused, with a reason for the person who asked. `status` is the HTTP answer."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def in_parts(db: Session, item: Item) -> int:
    """Pieces of this item out in parts right now, read off its log."""
    return replay_item(db, item, load_vocabulary(db)).in_parts_qty


def _moved_between_phases(db: Session, item: Item) -> bool:
    return (
        db.scalar(
            sa.select(sa.func.count()).select_from(Event).where(
                Event.item_id == item.id, (Event.from_link.is_(True)) | (Event.to_link.is_(True))
            )
        )
        > 0
    )


def _settled(db: Session, item: Item, doing: str) -> None:
    out = in_parts(db, item)
    if out:
        raise PartsError(
            f"{out} piece{'' if out == 1 else 's'} of {item.code} are out in parts; "
            f"assemble them before you {doing}"
        )


def add_part(db: Session, parent: Item, code: str, description: str, per_unit: int,
             dimensions: str | None = None) -> Item:
    if not parent.is_active:
        raise PartsError(f"{parent.code} is archived")
    code = (code or "").strip()
    if not code:
        raise PartsError("a part needs a code", 422)
    if per_unit < 1:
        raise PartsError("a part goes into each piece at least once", 422)
    if parent.parent_item_id is None and not parts_of(db, parent.id) and _moved_between_phases(db, parent):
        raise PartsError(
            f"units of {parent.code} have been moved between phases; give parts to each "
            "phase's batch before moving units between them"
        )
    _settled(db, parent, "change its parts")
    lock_project(db, parent.project_id)
    if identity_taken(db, parent.project_id, code, parent.area_id, parent.phase_id):
        raise PartsError(f"{code} already exists under the same area and phase")
    part = Item(
        id=uuid.uuid4(),
        code=code,
        project_id=parent.project_id,
        area_id=parent.area_id,
        phase_id=parent.phase_id,
        description=(description or "").strip(),
        dimensions=(dimensions or "").strip() or None,
        # A part has no batch of its own: its units arrive by splitting the
        # piece it belongs to, and leave by assembling it again.
        total_qty=0,
        sample_qty=0,
        drawing_revision=parent.drawing_revision,
        parent_item_id=parent.id,
        per_unit=per_unit,
    )
    db.add(part)
    db.flush()
    return part


def change_part(db: Session, part: Item, per_unit: int | None = None,
                description: str | None = None) -> Item:
    if part.parent_item_id is None:
        raise PartsError(f"{part.code} is not a part of anything", 422)
    parent = db.get(Item, part.parent_item_id)
    if per_unit is not None and per_unit != part.per_unit:
        if per_unit < 1:
            raise PartsError("a part goes into each piece at least once", 422)
        if parent is not None:
            _settled(db, parent, "change how many of a part go into each piece")
        part.per_unit = per_unit
    if description is not None:
        part.description = description.strip()
    db.flush()
    return part


def guard_removal(db: Session, item: Item, purge: bool) -> None:
    """Archiving or deleting an item that is a part, or has parts."""
    if item.parent_item_id is not None:
        parent = db.get(Item, item.parent_item_id)
        if parent is not None and parent.is_active:
            _settled(db, parent, "take one of its parts away")
    if purge and db.scalar(
        sa.select(sa.func.count()).select_from(Item).where(Item.parent_item_id == item.id)
    ):
        raise PartsError(f"{item.code} has parts; delete them first")
    if purge and db.scalar(
        sa.select(sa.func.count()).select_from(Item).where(Item.sample_item_id == item.id)
    ):
        raise PartsError(
            f"another phase's batch of {item.code} waits on this item's sample; delete or "
            "re-file that batch first"
        )


def descendants(db: Session, item: Item) -> list[Item]:
    out: list[Item] = []
    frontier = [item.id]
    while frontier:
        children = list(db.scalars(sa.select(Item).where(Item.parent_item_id.in_(frontier))))
        out.extend(children)
        frontier = [child.id for child in children]
    return out


def follow_parent(db: Session, item: Item) -> None:
    """Parts are filed where the piece they belong to is: same project, area
    and phase. Called after any of those change on an item with parts."""
    for part in descendants(db, item):
        if (part.project_id, part.area_id, part.phase_id) == (item.project_id, item.area_id, item.phase_id):
            continue
        if identity_taken(db, item.project_id, part.code, item.area_id, item.phase_id, part.id):
            raise PartsError(f"its part {part.code} would land on another item with the same code there")
        part.project_id, part.area_id, part.phase_id = item.project_id, item.area_id, item.phase_id
    db.flush()


# -- phases --------------------------------------------------------------------


def _move_type(db: Session) -> EventType:
    found = db.scalars(
        sa.select(EventType).where(
            EventType.moves_quantity.is_(True),
            EventType.is_rework.is_(False),
            EventType.is_correction.is_(False),
            EventType.is_finish.is_(False),
            EventType.is_active.is_(True),
        ).order_by(EventType.sort_order).limit(1)
    ).first()
    if found is None:
        raise PartsError("no movement event type is seeded", 500)
    return found


def split_phase(db: Session, item: Item, phase_id: uuid.UUID | None,
                take: dict[str, int], user: User) -> Item:
    """Re-file some of `item`'s units into another phase. Returns the other
    phase's batch. `take` is how many from each place: "unstarted" or a stage
    id."""
    if not item.is_active:
        raise PartsError(f"{item.code} is archived")
    if item.parent_item_id is not None or parts_of(db, item.id):
        raise PartsError(
            "an item split into parts moves between phases as a whole piece; split it "
            "across phases before giving it parts"
        )
    if phase_id == item.phase_id:
        raise PartsError("those units are in that phase already", 422)
    phase = db.get(Phase, phase_id) if phase_id is not None else None
    if phase_id is not None and (phase is None or phase.project_id != item.project_id):
        raise PartsError("pick one of this project's phases", 422)
    if phase is not None and not phase.is_active:
        raise PartsError(f"phase '{phase.name}' is retired")
    take = {key: qty for key, qty in take.items() if qty}
    if not take:
        raise PartsError("say how many to move", 422)
    if any(qty < 0 for qty in take.values()):
        raise PartsError("quantities are positive", 422)

    lock_project(db, item.project_id)
    target = db.scalars(
        sa.select(Item).where(
            Item.project_id == item.project_id,
            Item.code == item.code,
            Item.area_id == item.area_id if item.area_id else Item.area_id.is_(None),
            Item.phase_id == phase_id if phase_id else Item.phase_id.is_(None),
        )
    ).first()
    if target is not None and not target.is_active:
        raise PartsError(f"{identity_label(item.code, None, phase)} exists but is archived; reactivate it first")
    if target is not None and (target.parent_item_id is not None or parts_of(db, target.id)):
        raise PartsError("the batch in that phase has parts; units move into it as whole pieces only")

    vocab = load_vocabulary(db)
    ledger = replay_item(db, item, vocab)

    def new_batch(total: int, sample_item_id) -> Item:
        made = Item(
            id=uuid.uuid4(), code=item.code, project_id=item.project_id, area_id=item.area_id,
            phase_id=phase_id, description=item.description, dimensions=item.dimensions,
            total_qty=total, sample_qty=0, drawing_revision=item.drawing_revision,
            sample_item_id=sample_item_id,
        )
        db.add(made)
        db.flush()
        return made

    if ledger.released_at is None:
        # Nothing of the batch is on the floor yet: this is planning.
        if set(take) != {UNSTARTED}:
            raise PartsError(
                "the batch is not released until the sample is done, so only its planned "
                "quantity can move", 422,
            )
        qty = take[UNSTARTED]
        if qty > item.total_qty:
            raise PartsError(f"the batch is {item.total_qty}; it cannot give {qty}", 422)
        item.total_qty -= qty
        root = item.sample_item_id or (item.id if item.sample_qty else None)
        if target is None:
            target = new_batch(qty, root)
        else:
            target.total_qty += qty
        db.flush()
        return target

    # Released: real units, wherever they are, moved as linked pairs.
    stages = {str(stage.id): stage for stage in db.scalars(sa.select(Stage))}
    at: dict[str, int] = {}
    for bucket in ledger.occupied(include_unstarted=True):
        key = UNSTARTED if bucket.position.is_unstarted else str(bucket.position.stage_id)
        at[key] = at.get(key, 0) + bucket.qty - bucket.sample_qty
    for key, qty in take.items():
        if key != UNSTARTED and key not in stages:
            raise PartsError("unknown stage", 422)
        if qty > at.get(key, 0):
            where = "not started" if key == UNSTARTED else stages[key].name
            raise PartsError(f"only {at.get(key, 0)} of the batch are at {where}", 422)

    if target is None:
        target = new_batch(0, None)
    move_type = _move_type(db)
    moment = utcnow()
    for offset, (key, qty) in enumerate(sorted(take.items()), start=0):
        link = uuid.uuid4()
        when = moment + timedelta(milliseconds=offset)
        stage_id = None if key == UNSTARTED else uuid.UUID(key)
        note = f"moved to {phase.name if phase else 'no phase'}"
        rows = [
            EventCreate(
                id=uuid.uuid4(), item_id=item.id, event_type_id=move_type.id, occurred_at=when,
                qty=qty, from_stage_id=stage_id, from_unstarted=stage_id is None,
                link_id=link, to_link=True, note=note, user_id=user.id,
            ),
            EventCreate(
                id=uuid.uuid4(), item_id=target.id, event_type_id=move_type.id, occurred_at=when,
                qty=qty, stage_id=stage_id, link_id=link, from_link=True,
                note=f"moved from {_phase_name(db, item.phase_id)}", user_id=user.id,
            ),
        ]
        try:
            record_group(db, rows, user)
        except EventRejected as exc:
            raise PartsError(exc.reason, 422) from None
    db.flush()
    return target


def _phase_name(db: Session, phase_id) -> str:
    phase = db.get(Phase, phase_id) if phase_id else None
    return phase.name if phase else "no phase"
