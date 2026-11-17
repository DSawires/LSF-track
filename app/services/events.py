"""Writing to the log.

Two rules govern this module.

Idempotency: the client generates the UUID, so a retry after a lost response finds
the row already there and returns it. Re-posting is never an error and never
duplicates.

Rejections are structural or arithmetic, never silent. An unknown id, an entry
that does not say where its units came from, or a move of more units than the
log holds at that place all return a 422 with a reason. Nothing is dropped
without the client being told: the phone quarantines a rejected entry where the
engineer can see the reason, retry it (the recovery path when the entry it
depends on syncs later from another device), or discard it. Batches are applied
in occurred_at order so an offline session's own chain of moves always
validates against itself.

An entry names a stage and a source: units move freely, from any place to any
stage or to Done. The older shape -- a step of the item's sequence, and no
source -- is still accepted, because a phone that has not reopened the app since
the change may be holding queued entries in it, and those must drain.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.ledger import ItemLedger, effective_events
from app.models import (
    Event,
    EventState,
    EventType,
    Item,
    ItemStep,
    ReasonCode,
    Stage,
    Station,
    User,
)

log = logging.getLogger(__name__)


# Hard ceiling on how far ahead of the server clock an occurred_at may be.
# Distinct from settings.max_device_ahead_seconds, which only *flags*.
_MAX_FUTURE = timedelta(days=7)


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

    # Row-locked until the request commits. The availability check below reads
    # the item's log and then inserts into it; without the lock two phones
    # posting for the same item at once would each validate against a log that
    # does not hold the other's entry, and both would be stored. (SQLite has no
    # row locks and ignores this; it serialises writers on its own.)
    item = db.get(Item, payload.item_id, with_for_update=True)
    if item is None:
        raise EventRejected("unknown item", "item_id")
    if not item.is_active:
        raise EventRejected("item is archived", "item_id")

    event_type = db.get(EventType, payload.event_type_id)
    if event_type is None or not event_type.is_active:
        raise EventRejected("unknown event type", "event_type_id")

    stage = None
    item_step = None
    if payload.stage_id is not None and payload.item_step_id is not None:
        raise EventRejected("an entry names a stage or a legacy step, not both", "stage_id")
    if event_type.is_finish and (payload.stage_id is not None or payload.item_step_id is not None):
        raise EventRejected("a finish entry moves units to Done and names no stage", "stage_id")
    if payload.stage_id is not None:
        stage = db.get(Stage, payload.stage_id)
        if stage is None:
            raise EventRejected("unknown stage", "stage_id")
    elif payload.item_step_id is not None:
        # Sequence-era payload, from a queue filled before the change.
        item_step = db.get(ItemStep, payload.item_step_id)
        if item_step is None:
            raise EventRejected("unknown item step", "item_step_id")
        if item_step.item_id != item.id:
            raise EventRejected("item step belongs to a different item", "item_step_id")
        stage = db.get(Stage, item_step.stage_id)
    elif event_type.requires_item_step:
        raise EventRejected("this entry needs a stage", "stage_id")

    # Where the units came from: a stage, "not started", or Done.
    sources = [
        payload.from_stage_id is not None,
        payload.from_unstarted,
        payload.from_done,
        payload.from_item_step_id is not None,
    ]
    if sum(sources) > 1:
        raise EventRejected("an entry takes its units from one place only", "from_stage_id")
    if any(sources) and not event_type.moves_quantity:
        raise EventRejected(
            "only an entry that moves units can say where they came from", "from_stage_id"
        )
    if payload.from_stage_id is not None and db.get(Stage, payload.from_stage_id) is None:
        raise EventRejected("unknown source stage", "from_stage_id")
    if payload.from_item_step_id is not None:
        from_step = db.get(ItemStep, payload.from_item_step_id)
        if from_step is None:
            raise EventRejected("unknown source step", "from_item_step_id")
        if from_step.item_id != item.id:
            raise EventRejected("source step belongs to a different item", "from_item_step_id")
    if payload.from_done and not event_type.is_rework:
        # Taking a finished unit back out of Done is rework, and has a reason.
        raise EventRejected("only rework takes units back out of Done", "from_done")
    legacy_shape = payload.item_step_id is not None or payload.from_item_step_id is not None
    if event_type.moves_quantity and not legacy_shape:
        if payload.stage_id is not None and not any(sources):
            raise EventRejected("say where these units are coming from", "from_stage_id")
        if (
            payload.stage_id is not None
            and payload.from_stage_id == payload.stage_id
            and payload.station_id is None
        ):
            # A move within one stage only means something as a change of station.
            raise EventRejected("these units are already at that stage", "stage_id")

    # States are gone from the floor: a unit is at a stage or it is not. A
    # state_id is still accepted because phones running the previous app
    # version may be holding queued entries that carry one, and those must
    # drain rather than strand in a quarantine nobody can resolve.
    if payload.state_id is not None and db.get(EventState, payload.state_id) is None:
        raise EventRejected("unknown state", "state_id")

    if payload.station_id is not None:
        station = db.get(Station, payload.station_id)
        if station is None:
            raise EventRejected("unknown station", "station_id")
        if stage is not None and station.stage_id != stage.id:
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
    if occurred_at > utcnow() + _MAX_FUTURE:
        # Small clock drift is stored and flagged (see clock_anomalies); a
        # timestamp days in the future is garbage that would sort after every
        # honest event for as long as it stayed in the log.
        raise EventRejected(
            "occurred_at is more than a week in the future; fix the device clock",
            "occurred_at",
        )

    _validate_availability(db, item, payload, event_type, occurred_at)

    event = Event(
        id=payload.id,
        item_id=item.id,
        **_movement_fields(payload),
        event_type_id=event_type.id,
        reason_code_id=payload.reason_code_id,
        occurred_at=occurred_at,
        # Never from the client: this is how a wrong device clock is detected.
        received_at=utcnow(),
        user_id=_attribute_user(db, payload, session_user),
        # Always the authenticated session, so the claimed attribution above is
        # auditable: the log records both who saw the work and who posted it.
        submitted_by_user_id=session_user.id,
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


_MOVEMENT_FIELDS = (
    "stage_id",
    "item_step_id",
    "station_id",
    "state_id",
    "qty",
    "from_stage_id",
    "from_unstarted",
    "from_done",
    "from_item_step_id",
)


def _movement_fields(payload) -> dict:
    """What the entry moves, to where, from where -- as stored on the row."""
    return {name: getattr(payload, name) for name in _MOVEMENT_FIELDS}


def _validate_availability(
    db, item: Item, payload, event_type: EventType, occurred_at: datetime
) -> None:
    """Reject a move the log cannot support.

    CLAUDE.md: moving more units than rest at the place they are taken from is
    a validation error, not a stored fact. The test is a simulation, not a
    running-total check: replay the item's whole log in occurred_at order *with
    the candidate included*, and reject only if that replay over-advances where
    the log without it did not. Judging the whole timeline matters because
    events sync out of order -- a move out of paint arriving before the move
    into paint must not be refused when the replay will seat both correctly
    once the partner lands.

    A genuine rejection is recoverable: the client quarantines the entry with
    this reason and offers a retry, which is how it resolves once the state of
    the log changes (a correction voids the conflicting entry, or the entry it
    depends on syncs in from another device).
    """
    if not event_type.moves_quantity or payload.qty <= 0:
        return

    # Replays this item's log as it stands, including events inserted earlier
    # in the same batch (they are in the session already). Cheap at this
    # factory's volume; see services.derivation for the scaling escape hatch.
    from app.services.derivation import load_vocabulary

    vocab = load_vocabulary(db)
    steps = list(db.scalars(sa.select(ItemStep).where(ItemStep.item_id == item.id)))
    events = list(db.scalars(sa.select(Event).where(Event.item_id == item.id)))

    # An entry with no source is the sequence-era shape: "the nearest units
    # before this step". That only means something on an item that has a
    # sequence. On any other item it would quietly take from "not started".
    unsourced = not (
        payload.from_stage_id is not None
        or payload.from_unstarted
        or payload.from_done
        or payload.from_item_step_id is not None
    )
    if unsourced and not steps:
        raise EventRejected("say where these units are coming from", "from_stage_id")

    # A detached stand-in, never added to the session: the replay only reads
    # attributes.
    candidate = Event(
        id=payload.id,
        item_id=item.id,
        **_movement_fields(payload),
        event_type_id=event_type.id,
        occurred_at=occurred_at,
        received_at=utcnow(),
        supersedes_event_id=payload.supersedes_event_id,
    )

    def replay(rows: list) -> ItemLedger:
        kept, _anomalies = effective_events(rows)
        ledger = ItemLedger(item, steps, vocab)
        ledger.apply_all(kept)
        return ledger

    # The same question the replay will ask of the stored row, asked first:
    # does this entry describe a move at all?
    move, problem = ItemLedger(item, steps, vocab).resolve(candidate, event_type)
    if move is None:
        # Point at the field the engineer would have to change.
        field = "from_item_step_id" if payload.from_item_step_id is not None else "stage_id"
        raise EventRejected(f"this entry cannot be placed: {problem}", field)

    before = [a for a in replay(events).anomalies if a.code == "over_advance"]
    with_candidate = replay(events + [candidate])
    after = [a for a in with_candidate.anomalies if a.code == "over_advance"]
    if len(after) > len(before):
        blame = next((a for a in after if a.event_id == payload.id), after[-1])
        raise EventRejected(f"the log cannot support this move: {blame.detail}", "qty")

    # allows_partial_qty=False: this stage takes whole lots, never a split.
    # Enforced as "nothing may be left behind": after the replay with this
    # event included, the place it drew from must be empty.
    target_stage = vocab.stages.get(with_candidate.positions[move.target].stage_id)
    if (
        target_stage is not None
        and not target_stage.allows_partial_qty
        and not event_type.is_rework
    ):
        sources = [i for i in move.sources if i != move.target]
        left_behind = with_candidate.qty_in(sources)
        if left_behind:
            raise EventRejected(
                (
                    f"stage '{target_stage.name}' does not allow partial quantities; "
                    f"this move would leave {left_behind} behind"
                ),
                "qty",
            )


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
    """Every field the client controls. A UUID reused with ANY different
    content is the client bug the divergent flag exists to catch -- including
    a different timestamp or a different superseded target."""
    fields = (
        "item_id",
        "stage_id",
        "item_step_id",
        "station_id",
        "event_type_id",
        "state_id",
        "qty",
        "from_stage_id",
        "from_item_step_id",
        "reason_code_id",
        "supersedes_event_id",
        "note",
    )
    for field in fields:
        if getattr(existing, field) != getattr(payload, field, None):
            return True
    for flag in ("from_unstarted", "from_done"):
        if bool(getattr(existing, flag)) != bool(getattr(payload, flag, False)):
            return True
    payload_occurred = getattr(payload, "occurred_at", None)
    if payload_occurred is not None and payload_occurred.tzinfo is not None:
        if existing.occurred_at != payload_occurred:
            return True
    # user_id=None means "the session user", which legitimately varies between
    # the original post and a replay drained by a colleague's phone -- only a
    # non-null claim that names someone else counts as divergence.
    claimed = getattr(payload, "user_id", None)
    if claimed is not None and existing.user_id != claimed:
        return True
    return False
