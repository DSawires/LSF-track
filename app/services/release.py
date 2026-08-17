"""Release: the handoff from the technical office to the floor.

Releasing copies the route template's steps onto the item. From that moment the
item's route is frozen: publishing a new version of the template, or inserting a
stage into it, changes nothing for work already in production. That is the whole
reason a stage can be added on a Tuesday afternoon without anyone holding their
breath.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from datetime import timedelta

from app.db import utcnow
from app.models import (
    Event,
    EventState,
    EventType,
    Item,
    ItemStep,
    RouteTemplate,
    RouteTemplateStep,
    Stage,
    User,
)


class ReleaseError(Exception):
    pass


def release_item(
    db: Session,
    item: Item,
    route_template: RouteTemplate,
    user: User,
    drawing_revision: str | None = None,
    released_at: datetime | None = None,
    initial_quantities: dict[int, int] | None = None,
) -> list[ItemStep]:
    if item.is_released:
        raise ReleaseError(f"{item.code} was already released on {item.released_at:%Y-%m-%d}")

    template_steps = list(
        db.scalars(
            sa.select(RouteTemplateStep)
            .where(RouteTemplateStep.route_template_id == route_template.id)
            .order_by(RouteTemplateStep.seq)
        )
    )
    if not template_steps:
        raise ReleaseError(f"route {route_template.code} v{route_template.version} has no steps")

    # Route creation validates this too, but the flag can be flipped on a stage
    # *after* templates referencing it exist. A mid-route terminal stage would
    # count units finished there and hide them from every later step.
    stages = {s.id: s for s in db.scalars(sa.select(Stage))}
    for template_step in template_steps[:-1]:
        stage = stages.get(template_step.stage_id)
        if stage is not None and stage.is_terminal:
            raise ReleaseError(
                f"stage '{stage.name}' is terminal but not the last step of "
                f"{route_template.code} v{route_template.version}; fix the route "
                f"or the stage flag before releasing against it"
            )

    revision = drawing_revision or item.drawing_revision
    moment = released_at or utcnow()

    steps = []
    for template_step in template_steps:
        step = ItemStep(
            item_id=item.id,
            seq=template_step.seq,
            stage_id=template_step.stage_id,
            created_at=moment,
        )
        db.add(step)
        steps.append(step)

    item.route_template_id = route_template.id
    item.released_at = moment
    item.released_by_user_id = user.id
    item.released_revision = revision
    item.drawing_revision = revision
    db.flush()

    release_type = _event_type(db, "is_release")
    if release_type is not None:
        db.add(
            Event(
                id=uuid.uuid4(),
                item_id=item.id,
                item_step_id=None,
                event_type_id=release_type.id,
                state_id=None,
                qty=0,
                occurred_at=moment,
                received_at=utcnow(),
                user_id=user.id,
                note=f"released at revision {revision} on route "
                f"{route_template.code} v{route_template.version}",
            )
        )
        db.flush()

    if initial_quantities:
        _place_initial_quantities(db, item, steps, initial_quantities, user, moment)
    return steps


def _place_initial_quantities(
    db: Session,
    item: Item,
    steps: list[ItemStep],
    quantities: dict[int, int],
    user: User,
    moment: datetime,
) -> None:
    """Onboard an item that is already mid-production.

    Each placement is an ordinary queued event, so the ledger needs no special
    case. Deepest step first, each a millisecond apart: the replay pulls from the
    *nearest* upstream position, and only this ordering guarantees every
    placement draws from the unstarted pool rather than from a shallower
    placement that happened to land first.
    """
    step_by_seq = {step.seq: step for step in steps}
    for seq, qty in quantities.items():
        if seq not in step_by_seq:
            raise ReleaseError(f"step {seq} is not on this route")
        if qty <= 0:
            raise ReleaseError(f"quantity for step {seq} must be positive")
    total = sum(quantities.values())
    if total > item.total_qty:
        raise ReleaseError(
            f"distributed {total} across stages but the batch is only {item.total_qty}"
        )

    move_type = db.scalars(
        sa.select(EventType).where(
            EventType.moves_quantity.is_(True),
            EventType.is_rework.is_(False),
            EventType.is_correction.is_(False),
            EventType.is_active.is_(True),
        ).order_by(EventType.sort_order).limit(1)
    ).first()
    queued_state = db.scalars(
        sa.select(EventState).where(EventState.is_active.is_(True))
        .order_by(EventState.is_initial.desc(), EventState.sort_order).limit(1)
    ).first()
    if move_type is None or queued_state is None:
        raise ReleaseError("no movement event type or entry state is seeded")

    for offset, seq in enumerate(sorted(quantities, reverse=True), start=1):
        db.add(
            Event(
                id=uuid.uuid4(),
                item_id=item.id,
                item_step_id=step_by_seq[seq].id,
                event_type_id=move_type.id,
                state_id=queued_state.id,
                qty=quantities[seq],
                occurred_at=moment + timedelta(milliseconds=offset),
                received_at=utcnow(),
                user_id=user.id,
                note="initial position at release",
            )
        )
    db.flush()


def bump_revision(db: Session, item: Item, revision: str, user: User) -> Event | None:
    """Issue a drawing revision after release.

    The point of recording it is that anything already in flight was built to the
    old drawing. The quantity affected is derived from the log, not stored.
    """
    if not item.is_released:
        raise ReleaseError("only a released item can have a revision bump")
    if revision == item.drawing_revision:
        raise ReleaseError(f"{item.code} is already at revision {revision}")

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
