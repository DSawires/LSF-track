"""Writing to the log.

Two rules govern this module.

Idempotency: the client generates the UUID, so a retry after a lost response finds
the row already there and returns it. Re-posting is never an error and never
duplicates.

Rejections are rare and structural. An event that reaches this function is stored
unless it is literally unprocessable -- an unknown id, a step that belongs to
another item. Arithmetic that does not add up (a batch advancing more units than
exist upstream) is *stored and flagged*, because the engineer logged it hours ago on
a phone with no signal and has long since walked away. Losing their entry at sync
time is the one failure this system cannot afford.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    Event,
    EventState,
    EventType,
    Item,
    ItemStep,
    ReasonCode,
    Station,
    User,
)

log = logging.getLogger(__name__)


class EventRejected(Exception):
    """The payload cannot be stored at all. Surfaces to the client as a 422."""

    def __init__(self, reason: str, field: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field


@dataclass
class EventWrite:
    event: Event
    created: bool
    divergent: bool = False


def record_event(db: Session, payload, session_user: User) -> EventWrite:
    existing = db.get(Event, payload.id)
    if existing is not None:
        divergent = _differs(existing, payload)
        if divergent:
            # The stored row wins. Two different bodies under one client-generated
            # UUID means a client bug, not a correction; corrections have their own
            # event type.
            log.warning(
                "event %s re-posted with a different body; returning the stored row",
                payload.id,
            )
        return EventWrite(event=existing, created=False, divergent=divergent)

    item = db.get(Item, payload.item_id)
    if item is None:
        raise EventRejected("unknown item", "item_id")
    if not item.is_released:
        raise EventRejected("item has not been released to production", "item_id")

    event_type = db.get(EventType, payload.event_type_id)
    if event_type is None or not event_type.is_active:
        raise EventRejected("unknown event type", "event_type_id")

    item_step = None
    if payload.item_step_id is not None:
        item_step = db.get(ItemStep, payload.item_step_id)
        if item_step is None:
            raise EventRejected("unknown item step", "item_step_id")
        if item_step.item_id != item.id:
            raise EventRejected("item step belongs to a different item", "item_step_id")
    elif event_type.requires_item_step:
        raise EventRejected("this event type needs a step", "item_step_id")

    state = None
    if payload.state_id is not None:
        state = db.get(EventState, payload.state_id)
        if state is None:
            raise EventRejected("unknown state", "state_id")
    elif event_type.moves_quantity:
        raise EventRejected("this event type needs a state", "state_id")

    if payload.station_id is not None:
        station = db.get(Station, payload.station_id)
        if station is None:
            raise EventRejected("unknown station", "station_id")
        if item_step is not None and station.stage_id != item_step.stage_id:
            raise EventRejected("station is not part of this stage", "station_id")

    if payload.reason_code_id is not None:
        if db.get(ReasonCode, payload.reason_code_id) is None:
            raise EventRejected("unknown reason code", "reason_code_id")
    elif event_type.requires_reason_code:
        raise EventRejected("this event type needs a reason code", "reason_code_id")

    if event_type.moves_quantity and payload.qty <= 0:
        raise EventRejected("quantity must be positive", "qty")
    if payload.qty < 0:
        raise EventRejected("quantity cannot be negative", "qty")

    if event_type.is_correction and payload.supersedes_event_id is None:
        raise EventRejected("a correction must name the event it supersedes", "supersedes_event_id")
    if payload.supersedes_event_id is not None:
        if not event_type.is_correction:
            raise EventRejected(
                "only a correction may supersede an event", "supersedes_event_id"
            )
        superseded = db.get(Event, payload.supersedes_event_id)
        if superseded is not None and superseded.item_id != item.id:
            raise EventRejected(
                "cannot supersede an event on another item", "supersedes_event_id"
            )
        # A correction whose target has not synced yet is fine: the log is a set,
        # and the target is excluded from the derivation the moment it lands.

    occurred_at = _require_aware(payload.occurred_at, "occurred_at")

    event = Event(
        id=payload.id,
        item_id=item.id,
        item_step_id=payload.item_step_id,
        station_id=payload.station_id,
        event_type_id=event_type.id,
        state_id=payload.state_id,
        qty=payload.qty,
        reason_code_id=payload.reason_code_id,
        occurred_at=occurred_at,
        # Never from the client: this is how a wrong device clock is detected.
        received_at=utcnow(),
        user_id=_attribute_user(db, payload, session_user),
        note=payload.note,
        supersedes_event_id=payload.supersedes_event_id,
    )
    try:
        # SAVEPOINT, not a plain flush: if two of the engineer's retries race, the
        # losing insert must roll back *alone*. A full rollback here would discard
        # earlier events in the same batch drain that the client is about to be
        # told were stored.
        with db.begin_nested():
            db.add(event)
    except sa.exc.IntegrityError:
        stored = db.get(Event, payload.id)
        if stored is None:
            raise
        return EventWrite(event=stored, created=False)
    return EventWrite(event=event, created=True)


def _attribute_user(db: Session, payload, session_user: User) -> uuid.UUID:
    """Credit the engineer who logged it, not whoever happened to drain the queue.

    Devices are shared on the floor: an event logged by one engineer can sync hours
    later under another's session. A client-supplied user is honoured when it names
    a real active user, which keeps the log truthful about who saw the work happen.
    """
    claimed = getattr(payload, "user_id", None)
    if claimed is None or claimed == session_user.id:
        return session_user.id
    user = db.get(User, claimed)
    if user is None or not user.is_active:
        return session_user.id
    return user.id


def _require_aware(moment: datetime, field: str) -> datetime:
    if moment.tzinfo is None:
        raise EventRejected(f"{field} must carry a timezone", field)
    return moment


def _differs(existing: Event, payload) -> bool:
    for field in ("item_id", "item_step_id", "station_id", "event_type_id", "state_id", "qty"):
        if getattr(existing, field) != getattr(payload, field, None):
            return True
    return False
