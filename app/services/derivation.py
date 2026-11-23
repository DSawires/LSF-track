"""Loading the log and replaying it.

Everything that needs to know where an item is goes through here. There is no
cache: at this factory's volume a replay is cheap, and a projection that can drift
from the log is a liability the design deliberately avoids. If replay ever becomes
too slow, the fix is a materialised view rebuilt by a documented command -- not a
status column on `items`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.ledger import (
    Anomaly,
    ItemLedger,
    Vocabulary,
    clock_anomalies,
    effective_events,
)
from app.models import (
    Area,
    Event,
    EventState,
    EventType,
    Item,
    ItemStep,
    Phase,
    ReasonCode,
    Stage,
    Station,
)


def load_vocabulary(db: Session) -> Vocabulary:
    # All rows, including inactive ones: deactivating a state or stage hides it
    # from the pickers, but historical events referencing it must keep deriving.
    return Vocabulary(
        states=list(db.scalars(sa.select(EventState))),
        event_types={et.id: et for et in db.scalars(sa.select(EventType))},
        stages={s.id: s for s in db.scalars(sa.select(Stage))},
    )


def parts_linked(db: Session, items) -> set[uuid.UUID]:
    """The items whose linked entries are splits and assemblies: those that
    are parts, and those that have parts."""
    ids = [item.id for item in items]
    if not ids:
        return set()
    parents = set(
        db.scalars(
            sa.select(Item.parent_item_id).where(Item.parent_item_id.in_(ids)).distinct()
        )
    )
    return {item.id for item in items if item.parent_item_id is not None or item.id in parents}


def replay_item(db: Session, item: Item, vocab: Vocabulary, extra: list | None = None,
                known_release: dict | None = None) -> ItemLedger:
    """One item's ledger, from its whole log (plus `extra` rows not stored yet),
    built exactly as a report would build it."""
    steps = list(db.scalars(sa.select(ItemStep).where(ItemStep.item_id == item.id).order_by(ItemStep.seq)))
    events = list(db.scalars(sa.select(Event).where(Event.item_id == item.id)))
    kept, _anomalies = effective_events(events + list(extra or []))
    ledger = ItemLedger(
        item, steps, vocab,
        parts_link=item.id in parts_linked(db, [item]),
        release_at=shared_release(db, item, vocab, known_release),
    )
    ledger.apply_all(kept)
    return ledger


def shared_release(db: Session, item: Item, vocab: Vocabulary, known: dict | None = None):
    """For a batch released by another item's sample, when that sample was
    done (None while it is not). None for every other item."""
    root_id = item.sample_item_id
    if root_id is None:
        return None
    if known is not None and root_id in known:
        return known[root_id]
    root = db.get(Item, root_id)
    if root is None:
        return None
    return replay_item(db, root, vocab).released_at


class Derivation:
    """The replayed state of a set of items, plus everything that did not add up."""

    def __init__(
        self,
        ledgers: dict[uuid.UUID, ItemLedger],
        items: dict[uuid.UUID, Item],
        vocab: Vocabulary,
        anomalies: list[Anomaly],
    ) -> None:
        self.ledgers = ledgers
        self.items = items
        self.vocab = vocab
        self.anomalies = anomalies

    def ledger_for(self, item_id: uuid.UUID) -> ItemLedger | None:
        return self.ledgers.get(item_id)


def derive(
    db: Session,
    item_ids: Sequence[uuid.UUID] | None = None,
    vocab: Vocabulary | None = None,
) -> Derivation:
    vocab = vocab or load_vocabulary(db)

    item_query = sa.select(Item).where(Item.is_active.is_(True))
    if item_ids is not None:
        if not item_ids:
            return Derivation({}, {}, vocab, [])
        item_query = item_query.where(Item.id.in_(item_ids))
    items = {item.id: item for item in db.scalars(item_query)}
    if not items:
        return Derivation({}, {}, vocab, [])

    steps_by_item: dict[uuid.UUID, list[ItemStep]] = {item_id: [] for item_id in items}
    step_rows = db.scalars(
        sa.select(ItemStep).where(ItemStep.item_id.in_(items.keys())).order_by(ItemStep.seq)
    )
    for step in step_rows:
        steps_by_item[step.item_id].append(step)

    events_by_item: dict[uuid.UUID, list[Event]] = {item_id: [] for item_id in items}
    event_rows = db.scalars(
        sa.select(Event).where(Event.item_id.in_(items.keys())).order_by(Event.occurred_at)
    )
    all_events: list[Event] = []
    for event in event_rows:
        events_by_item[event.item_id].append(event)
        all_events.append(event)

    settings = get_settings()
    # A wrong clock or a late sync is news when it happens, and there is
    # nothing to do about it afterwards -- no correction clears it. Left
    # unbounded these flags only accumulate, and bury the ones that describe
    # something still wrong (an over-advance, a competing correction), which
    # are never windowed.
    recent = all_events
    if settings.clock_flag_days > 0:
        cutoff = utcnow() - timedelta(days=settings.clock_flag_days)
        recent = [event for event in all_events if event.received_at >= cutoff]
    anomalies: list[Anomaly] = clock_anomalies(
        recent, settings.max_device_ahead_seconds, settings.max_sync_lag_days
    )

    linked = parts_linked(db, list(items.values()))
    ledgers: dict[uuid.UUID, ItemLedger] = {}
    released: dict[uuid.UUID, object] = {}
    # Batches that share another item's sample go last: their release is read
    # off that item's replay.
    for item_id, item in sorted(items.items(), key=lambda pair: pair[1].sample_item_id is not None):
        kept, correction_anomalies = effective_events(events_by_item[item_id])
        anomalies.extend(correction_anomalies)
        ledger = ItemLedger(
            item, steps_by_item[item_id], vocab,
            parts_link=item_id in linked,
            release_at=shared_release(db, item, vocab, released),
        )
        ledger.apply_all(kept)
        released[item_id] = ledger.released_at
        anomalies.extend(ledger.anomalies)
        ledgers[item_id] = ledger

    return Derivation(ledgers, items, vocab, anomalies)


def reference_maps(db: Session) -> dict[str, dict[uuid.UUID, object]]:
    """Lookup rows keyed by id, for labelling report rows."""
    return {
        "stages": {row.id: row for row in db.scalars(sa.select(Stage))},
        "stations": {row.id: row for row in db.scalars(sa.select(Station))},
        "states": {row.id: row for row in db.scalars(sa.select(EventState))},
        "event_types": {row.id: row for row in db.scalars(sa.select(EventType))},
        "reason_codes": {row.id: row for row in db.scalars(sa.select(ReasonCode))},
        "areas": {row.id: row for row in db.scalars(sa.select(Area))},
        "phases": {row.id: row for row in db.scalars(sa.select(Phase))},
    }
