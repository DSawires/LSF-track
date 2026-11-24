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
from datetime import datetime, timedelta, timezone

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


def record_event(db: Session, payload, session_user: User, in_group: bool = False) -> EventWrite:
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
    elif event_type.requires_item_step and not (payload.from_link or payload.to_link):
        # A linked row names no stage when its units leave this item, or
        # arrive not started.
        raise EventRejected("this entry needs a stage", "stage_id")

    # Linked rows belong to a group, and the group is the unit of writing.
    if (payload.from_link or payload.to_link) and payload.link_id is None:
        raise EventRejected("a linked entry carries the link_id of its group", "link_id")
    if payload.link_id is not None and not (payload.from_link or payload.to_link):
        raise EventRejected("only a linked entry carries a link_id", "link_id")
    if payload.link_id is not None and not in_group:
        raise EventRejected(
            "a split, an assembly or a move into another phase is posted as one batch, all its entries together",
            "link_id",
        )
    if payload.to_link and (payload.stage_id is not None or event_type.is_finish):
        raise EventRejected("units leaving for another item name no stage here", "stage_id")
    if (payload.from_link or payload.to_link) and (event_type.is_rework or event_type.is_correction):
        raise EventRejected("a linked entry is a plain move, not rework or a correction", "event_type_id")

    # Where the units came from: a stage, "not started", Done, or another item.
    sources = [
        payload.from_stage_id is not None,
        payload.from_unstarted,
        payload.from_done,
        payload.from_item_step_id is not None,
        payload.from_link,
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
    if payload.from_done and not event_type.is_rework and not payload.to_link:
        # Taking a finished unit back out of Done is rework, and has a reason.
        # Finished parts going into an assembly are not coming back: they are
        # used up.
        raise EventRejected("only rework takes units back out of Done", "from_done")
    legacy_shape = payload.item_step_id is not None or payload.from_item_step_id is not None
    if (payload.from_link or payload.to_link) and not event_type.moves_quantity:
        raise EventRejected("a linked entry moves units", "event_type_id")
    if payload.to_link and not any(sources):
        raise EventRejected("say where these units are coming from", "from_stage_id")
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
    "link_id",
    "from_link",
    "to_link",
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
    the log without it did not. Judging the whole timeline, rather than what
    is there "now", is what lets an offline chain of moves validate against
    itself whatever order its entries arrive in.

    The entry is judged against the log as it stands. A move out of paint that
    reaches the server before the move into paint (logged on another phone,
    not yet synced) is refused like any other shortfall.

    A rejection is recoverable: the client quarantines the entry with this
    reason and offers a retry, which is how it resolves once the state of the
    log changes (the entry it depends on syncs in from another device, or a
    correction voids the conflicting one).
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
        or payload.from_link
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

    from app.services.derivation import parts_linked, shared_release

    options = {
        "parts_link": item.id in parts_linked(db, [item]),
        "release_at": shared_release(db, item, vocab),
    }

    def replay(rows: list) -> ItemLedger:
        kept, _anomalies = effective_events(rows)
        ledger = ItemLedger(item, steps, vocab, **options)
        ledger.apply_all(kept)
        return ledger

    # The same question the replay will ask of the stored row, asked first:
    # does this entry describe a move at all?
    move, problem = ItemLedger(item, steps, vocab, **options).resolve(candidate, event_type)
    if move is None:
        # Point at the field the engineer would have to change.
        field = "from_item_step_id" if payload.from_item_step_id is not None else "stage_id"
        raise EventRejected(f"this entry cannot be placed: {problem}", field)

    before = [a for a in replay(events).anomalies if a.code == "over_advance"]
    with_candidate = replay(events + [candidate])
    after = [a for a in with_candidate.anomalies if a.code == "over_advance"]
    if len(after) > len(before):
        blame = next((a for a in after if a.event_id == payload.id), None)
        raise EventRejected(
            _shortfall_reason(payload, blame, vocab, with_candidate.released_at is None), "qty"
        )

    # allows_partial_qty=False: this stage takes whole lots, never a split.
    # Enforced as "nothing may be left behind": after the replay with this
    # event included, the place it drew from must be empty.
    if move.target < 0 or move.inflow:
        return
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


def _shortfall_reason(payload, blame, vocab, sample_running: bool) -> str:
    """Why a move the log cannot hold was refused, in the engineer's words.

    This is read on a phone on the floor, by the person who has to choose
    between Retry and Discard: it says how many units are at the place the
    entry takes them from, and how many the entry moves. `blame` is the
    over-advance the replay found on this entry -- or None when this entry
    fits and it is one already in the log that no longer would.
    `sample_running`: the item's sample is not Done yet, so its batch is not
    on the floor.
    """
    if blame is None or blame.shortfall is None:
        # Dated before an entry already stored, and taking units that entry
        # went on to move: the two cannot both be true.
        return (
            "an entry already in the log, dated after this one, moved these units on; "
            "they cannot both be right"
        )
    there = payload.qty - blame.shortfall
    count = "none" if there == 0 else f"only {there}"
    verb = "is" if there == 1 else "are"
    if payload.from_unstarted:
        place = "still not started"
    elif payload.from_done:
        place = "in Done"
    elif payload.from_stage_id is not None:
        stage = vocab.stages.get(payload.from_stage_id)
        place = f"at {stage.name}" if stage is not None else "at that stage"
    else:
        # A sequence-era entry: it names a step, or nothing at all.
        place = "at the place it takes them from"
    reason = f"{count} {verb} {place}, and this entry moves {payload.qty}"
    # Drawing on "not started" -- by name, or as a sequence-era entry that
    # names no source and reaches back to it -- while the sample is still
    # running: the usual cause, and not one the numbers explain on their own.
    unsourced = not (payload.from_done or payload.from_stage_id or payload.from_item_step_id)
    if sample_running and (payload.from_unstarted or unsourced):
        reason += "; the batch is released when the sample is Done"
    return reason


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
        "link_id",
    )
    for field in fields:
        if getattr(existing, field) != getattr(payload, field, None):
            return True
    for flag in ("from_unstarted", "from_done", "from_link", "to_link"):
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


# -- linked entries -----------------------------------------------------------


def parts_of(db: Session, item_id: uuid.UUID) -> list[Item]:
    """An item's parts, in the order they were added."""
    return list(
        db.scalars(
            sa.select(Item)
            .where(Item.parent_item_id == item_id, Item.is_active.is_(True))
            .order_by(Item.created_at, Item.code)
        )
    )


def link_kind(db: Session, rows) -> str:
    """What a group of linked rows is -- "split", "assembly" or "transfer" -- or
    an EventRejected saying why it is none of them.

    The quantities have to agree, by the per-piece counts the parts have now:
    a split of 10 beds is 10 frames, 10 headboards and 40 legs, or it is not
    stored at all.
    """
    if len({row.item_id for row in rows}) != len(rows):
        raise EventRejected("a split or an assembly names each item once", "item_id")
    if len({_aware(row.occurred_at) for row in rows}) != 1:
        raise EventRejected("the entries of one split or assembly happen at one moment", "occurred_at")
    items = {row.item_id: db.get(Item, row.item_id) for row in rows}
    if any(item is None for item in items.values()):
        raise EventRejected("unknown item", "item_id")
    if len({item.project_id for item in items.values()}) != 1:
        raise EventRejected("a split or an assembly stays within one project", "item_id")
    outs = [row for row in rows if row.to_link]
    ins = [row for row in rows if row.from_link]
    if not outs or not ins or len(outs) + len(ins) != len(rows):
        raise EventRejected("units leave one side of a link and arrive on the other", "link_id")

    def matches(whole_row, part_rows) -> bool:
        parts = parts_of(db, whole_row.item_id)
        if not parts or {part.id for part in parts} != {row.item_id for row in part_rows}:
            return False
        per = {part.id: part.per_unit for part in parts}
        return all(row.qty == whole_row.qty * per[row.item_id] for row in part_rows)

    if len(outs) == 1 and matches(outs[0], ins):
        return "split"
    if len(ins) == 1 and matches(ins[0], outs):
        return "assembly"
    if len(outs) == 1 and len(ins) == 1:
        a, b = items[outs[0].item_id], items[ins[0].item_id]
        plain = all(
            item.parent_item_id is None and not parts_of(db, item.id) for item in (a, b)
        )
        if plain and a.code == b.code and outs[0].qty == ins[0].qty:
            return "transfer"
    raise EventRejected(
        "these entries do not add up to a split into parts, an assembly or a move into "
        "another phase: every part, at its count per piece, or nothing",
        "qty",
    )


def record_group(db: Session, rows, session_user: User) -> list[EventWrite]:
    """Store a split, an assembly or a phase move: every row, or none.

    A row already stored makes this a retry; the stored rows are returned as
    they are. Rows are written outgoing first, so the units an assembly uses
    up are checked before the finished pieces appear.
    """
    stored = [db.get(Event, row.id) for row in rows]
    if all(event is not None for event in stored):
        return [EventWrite(event=event, created=False) for event in stored]
    link_kind(db, rows)
    ordered = sorted(rows, key=lambda row: (not row.to_link, str(row.item_id)))
    written: dict[uuid.UUID, EventWrite] = {}
    with db.begin_nested():
        for row in ordered:
            written[row.id] = record_event(db, row, session_user, in_group=True)
    return [written[row.id] for row in rows]


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
