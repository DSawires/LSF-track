"""Derivation of current state from the event log.

Nothing here reads a stored status column, because none exists. Every number the
application reports is produced by replaying `events` through this module.

The model is a linear chain of positions per item:

    unstarted -> (step 10, queued) -> (step 10, in_progress) -> (step 10, completed)
              -> (step 20, queued) -> ...

Each position holds FIFO lots of units. An event moves `qty` units *into* the
position it names, pulling them from the nearest positions upstream (or, when the
event type is flagged as rework, from the nearest positions downstream). Skipped
intermediate logs -- an engineer who records `completed` without ever recording
`in_progress` -- resolve themselves, because the pull walks as far back as it needs.

Ordering of states and steps comes from `sort_order` and `seq`. Behaviour comes from
flags on the row. No function in this module branches on a stage, state or event
type *code*; adding a stage or an event type must never require editing this file.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models import Event, EventState, EventType, Item, ItemStep, Stage

UNSTARTED_INDEX = 0


@dataclass
class Lot:
    """A parcel of units that arrived at a position together."""

    qty: int
    arrived_at: datetime
    station_id: uuid.UUID | None = None
    rework_count: int = 0


@dataclass(frozen=True)
class Position:
    index: int
    item_step_id: uuid.UUID | None
    stage_id: uuid.UUID | None
    seq: int | None
    state_id: uuid.UUID | None
    is_final_complete: bool

    @property
    def is_unstarted(self) -> bool:
        return self.index == UNSTARTED_INDEX


@dataclass
class Anomaly:
    """Something the log says that does not add up.

    New writes that over-advance are rejected at the API (see services.events),
    but the log can still contain rows that predate that rule, arrived through
    another path, or conflict only in hindsight after a correction. Replay flags
    those rather than guessing; history is never rewritten to make them add up.
    """

    code: str
    event_id: uuid.UUID | None
    item_id: uuid.UUID
    detail: str
    occurred_at: datetime | None = None


@dataclass
class Bucket:
    """Units resting at one position, with the age of the oldest of them."""

    position: Position
    qty: int
    oldest_arrived_at: datetime
    reworked_qty: int
    by_station: dict[uuid.UUID | None, int] = field(default_factory=dict)


@dataclass
class Vocabulary:
    """The table-driven vocabularies, loaded once per report."""

    states: list[EventState]
    event_types: dict[uuid.UUID, EventType]
    stages: dict[uuid.UUID, Stage]

    @property
    def ordered_states(self) -> list[EventState]:
        return sorted(self.states, key=lambda s: (s.sort_order, s.code))


def effective_events(events: list[Event]) -> tuple[list[Event], list[Anomaly]]:
    """Drop superseded events and order what remains.

    An event named by *any* correction is dead, whether or not that correction was
    itself later corrected: a chain A <- B <- C leaves only C standing.
    """
    anomalies: list[Anomaly] = []
    superseders: dict[uuid.UUID, list[Event]] = {}
    for event in events:
        if event.supersedes_event_id is not None:
            superseders.setdefault(event.supersedes_event_id, []).append(event)

    excluded: set[uuid.UUID] = set(superseders.keys())

    # Two devices correcting the same event would otherwise both apply, and the
    # quantity would move twice. Keep the last one and say so.
    for target_id, correctors in superseders.items():
        if len(correctors) < 2:
            continue
        ordered = sorted(correctors, key=_sort_key)
        for loser in ordered[:-1]:
            excluded.add(loser.id)
            anomalies.append(
                Anomaly(
                    code="competing_correction",
                    event_id=loser.id,
                    item_id=loser.item_id,
                    detail=(
                        f"superseded by a later correction of the same event "
                        f"({target_id}); ignored in the derivation"
                    ),
                    occurred_at=loser.occurred_at,
                )
            )

    kept = [e for e in events if e.id not in excluded]
    return sorted(kept, key=_sort_key), anomalies


def _sort_key(event: Event) -> tuple:
    # occurred_at drives reports; received_at and id only break ties so that a
    # replay is deterministic regardless of insertion order.
    return (event.occurred_at, event.received_at, str(event.id))


class ItemLedger:
    """Replays one item's events into positions holding FIFO lots."""

    def __init__(self, item: Item, steps: list[ItemStep], vocab: Vocabulary) -> None:
        self.item = item
        self.vocab = vocab
        self.anomalies: list[Anomaly] = []
        self.applied_count = 0
        self.last_event_at: datetime | None = None

        self.positions: list[Position] = []
        self._index_by_key: dict[tuple[uuid.UUID | None, uuid.UUID | None], int] = {}

        start = item.released_at or item.created_at
        self.positions.append(
            Position(
                index=UNSTARTED_INDEX,
                item_step_id=None,
                stage_id=None,
                seq=None,
                state_id=None,
                is_final_complete=False,
            )
        )
        self._index_by_key[(None, None)] = UNSTARTED_INDEX

        ordered_steps = sorted(steps, key=lambda s: s.seq)
        ordered_states = vocab.ordered_states
        # The entry state (the queue) is exempt from the station requirement:
        # the queue in front of the paint stations is shared, and which booth the
        # work lands at isn't known until someone starts it.
        self._entry_state_id = ordered_states[0].id if ordered_states else None
        last_seq = ordered_steps[-1].seq if ordered_steps else None
        self._stage_by_step: dict[uuid.UUID, Stage] = {}
        for step in ordered_steps:
            stage = vocab.stages.get(step.stage_id)
            if stage is not None:
                self._stage_by_step[step.id] = stage
            for state in ordered_states:
                index = len(self.positions)
                is_final_complete = bool(
                    state.is_complete
                    and (step.seq == last_seq or (stage is not None and stage.is_terminal))
                )
                self.positions.append(
                    Position(
                        index=index,
                        item_step_id=step.id,
                        stage_id=step.stage_id,
                        seq=step.seq,
                        state_id=state.id,
                        is_final_complete=is_final_complete,
                    )
                )
                self._index_by_key[(step.id, state.id)] = index

        self.buckets: list[deque[Lot]] = [deque() for _ in self.positions]
        if item.total_qty > 0:
            self.buckets[UNSTARTED_INDEX].append(Lot(qty=item.total_qty, arrived_at=start))

    # -- replay ---------------------------------------------------------------

    def apply_all(self, events: list[Event]) -> None:
        for event in events:
            self.apply(event)

    def apply(self, event: Event) -> None:
        self.applied_count += 1
        if self.last_event_at is None or event.occurred_at > self.last_event_at:
            self.last_event_at = event.occurred_at

        event_type = self.vocab.event_types.get(event.event_type_id)
        if event_type is None:
            self._flag(event, "unknown_event_type", "event type row is missing")
            return

        stage = (
            self._stage_by_step.get(event.item_step_id)
            if event.item_step_id is not None
            else None
        )
        if (
            stage is not None
            and stage.requires_station
            and event.station_id is None
            and event.state_id != self._entry_state_id
        ):
            self._flag(
                event,
                "missing_station",
                f"stage {stage.code} records stations but this event has none",
            )

        if not event_type.moves_quantity:
            # Item-level events (a revision bump, say) are history, not movement.
            return

        target_index = self._index_by_key.get((event.item_step_id, event.state_id))
        if target_index is None:
            self._flag(
                event,
                "unknown_position",
                "event names a step or state that is not on this item's route",
            )
            return

        if event.qty <= 0:
            return

        if event_type.is_rework:
            # Rework is forward motion: the units are downstream and come back.
            sources = range(target_index + 1, len(self.positions))
        else:
            sources = range(target_index - 1, -1, -1)

        taken, shortfall = self._take(sources, event.qty)
        if shortfall:
            self._flag(
                event,
                "over_advance",
                (
                    f"event moves {event.qty} but only {event.qty - shortfall} "
                    f"were available at the preceding position; {shortfall} unaccounted for"
                ),
            )

        destination = self.buckets[target_index]
        rework_bump = 1 if event_type.is_rework else 0
        for lot in taken:
            destination.append(
                Lot(
                    qty=lot.qty,
                    arrived_at=event.occurred_at,
                    # Exactly what the event says, never inherited from the source
                    # lot: a stage without stations must not show its units parked
                    # at the previous stage's station.
                    station_id=event.station_id,
                    rework_count=lot.rework_count + rework_bump,
                )
            )

    def _take(self, source_indexes, qty: int) -> tuple[list[Lot], int]:
        taken: list[Lot] = []
        remaining = qty
        for index in source_indexes:
            lots = self.buckets[index]
            while lots and remaining > 0:
                lot = lots[0]
                if lot.qty <= remaining:
                    lots.popleft()
                    taken.append(lot)
                    remaining -= lot.qty
                else:
                    lot.qty -= remaining
                    taken.append(
                        Lot(
                            qty=remaining,
                            arrived_at=lot.arrived_at,
                            station_id=lot.station_id,
                            rework_count=lot.rework_count,
                        )
                    )
                    remaining = 0
            if remaining == 0:
                break
        return taken, remaining

    def _flag(self, event: Event, code: str, detail: str) -> None:
        self.anomalies.append(
            Anomaly(
                code=code,
                event_id=event.id,
                item_id=self.item.id,
                detail=detail,
                occurred_at=event.occurred_at,
            )
        )

    # -- reading it back ------------------------------------------------------

    def occupied(self, include_unstarted: bool = True, include_finished: bool = False):
        """Positions currently holding units, in route order."""
        result: list[Bucket] = []
        for position in self.positions:
            lots = self.buckets[position.index]
            if not lots:
                continue
            if position.is_unstarted and not include_unstarted:
                continue
            if position.is_final_complete and not include_finished:
                continue
            by_station: dict[uuid.UUID | None, int] = {}
            for lot in lots:
                by_station[lot.station_id] = by_station.get(lot.station_id, 0) + lot.qty
            result.append(
                Bucket(
                    position=position,
                    qty=sum(lot.qty for lot in lots),
                    oldest_arrived_at=min(lot.arrived_at for lot in lots),
                    reworked_qty=sum(lot.qty for lot in lots if lot.rework_count > 0),
                    by_station=by_station,
                )
            )
        return result

    def has_position(self, item_step_id: uuid.UUID | None, state_id: uuid.UUID | None) -> bool:
        return (item_step_id, state_id) in self._index_by_key

    def qty_at(self, item_step_id: uuid.UUID | None, state_id: uuid.UUID | None) -> int:
        index = self._index_by_key.get((item_step_id, state_id))
        if index is None:
            return 0
        return sum(lot.qty for lot in self.buckets[index])

    @property
    def completed_qty(self) -> int:
        return sum(
            lot.qty
            for position in self.positions
            if position.is_final_complete
            for lot in self.buckets[position.index]
        )

    @property
    def unstarted_qty(self) -> int:
        return sum(lot.qty for lot in self.buckets[UNSTARTED_INDEX])

    def available_upstream(
        self, item_step_id: uuid.UUID, state_id: uuid.UUID, is_rework: bool = False
    ) -> int:
        """How many units the next event at this position could legitimately move.

        The phone uses this to default the quantity field and to warn before the
        engineer submits something the log cannot support.
        """
        target = self._index_by_key.get((item_step_id, state_id))
        if target is None:
            return 0
        if is_rework:
            indexes = range(target + 1, len(self.positions))
        else:
            indexes = range(target - 1, -1, -1)
        return sum(lot.qty for index in indexes for lot in self.buckets[index])


def clock_anomalies(
    events: list[Event],
    max_device_ahead_seconds: int,
    max_sync_lag_days: int,
) -> list[Anomaly]:
    """Device clocks lie and phones sync late. Say which, rather than trusting either."""
    found: list[Anomaly] = []
    ahead = timedelta(seconds=max_device_ahead_seconds)
    lag = timedelta(days=max_sync_lag_days)
    for event in events:
        drift = event.occurred_at - event.received_at
        if drift > ahead:
            found.append(
                Anomaly(
                    code="device_clock_ahead",
                    event_id=event.id,
                    item_id=event.item_id,
                    detail=(
                        f"occurred_at is {_humanise(drift)} after the server received it; "
                        f"the device clock is probably wrong"
                    ),
                    occurred_at=event.occurred_at,
                )
            )
        elif -drift > lag:
            found.append(
                Anomaly(
                    code="late_sync",
                    event_id=event.id,
                    item_id=event.item_id,
                    detail=(
                        f"reached the server {_humanise(-drift)} after it was logged; "
                        f"reports for that period have changed"
                    ),
                    occurred_at=event.occurred_at,
                )
            )
    return found


def _humanise(delta: timedelta) -> str:
    seconds = int(abs(delta.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
