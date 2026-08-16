"""Loading the log and replaying it.

Everything that needs to know where an item is goes through here. There is no
cache: at this factory's volume a replay is cheap, and a projection that can drift
from the log is a liability the design deliberately avoids. If replay ever becomes
too slow, the fix is a materialised view rebuilt by a documented command -- not a
status column on `items`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import get_settings
from app.ledger import (
    Anomaly,
    ItemLedger,
    Vocabulary,
    clock_anomalies,
    effective_events,
)
from app.models import Event, EventState, EventType, Item, ItemStep, ReasonCode, Stage, Station


def load_vocabulary(db: Session) -> Vocabulary:
    return Vocabulary(
        states=list(db.scalars(sa.select(EventState).where(EventState.is_active.is_(True)))),
        event_types={et.id: et for et in db.scalars(sa.select(EventType))},
        stages={s.id: s for s in db.scalars(sa.select(Stage))},
    )


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

    item_query = sa.select(Item).where(
        Item.released_at.is_not(None), Item.is_active.is_(True)
    )
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
    anomalies: list[Anomaly] = clock_anomalies(
        all_events, settings.max_device_ahead_seconds, settings.max_sync_lag_days
    )

    ledgers: dict[uuid.UUID, ItemLedger] = {}
    for item_id, item in items.items():
        kept, correction_anomalies = effective_events(events_by_item[item_id])
        anomalies.extend(correction_anomalies)
        ledger = ItemLedger(item, steps_by_item[item_id], vocab)
        ledger.apply_all(kept)
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
    }


def ids(rows: Iterable) -> list[uuid.UUID]:
    return [row.id for row in rows]
