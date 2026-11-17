"""Derivation of current state from the event log.

Nothing here reads a stored status column, because none exists. Every number the
application reports is produced by replaying `events` through this module.

The model is a linear chain of positions per item:

    not started -> step 10 -> step 20 -> ... -> Done

A unit is at a stage or it is not; there is no queued / in progress / completed
within a stage. Each position holds FIFO lots of units. An event moves `qty`
units *into* the position it names, pulling them from the nearest positions
upstream (or, when the event type is flagged as rework, from the nearest
positions downstream). Skipped logs -- units logged at veneer that were never
logged at carpentry -- resolve themselves, because the pull walks as far back as
it needs. An entry may instead name where its units came from
(`from_item_step_id`, or `from_done`), and then only that place is drawn from:
that is what keeps "QC sent 5 back to carpentry" from taking the 5 out of a lot
resting at paint. Units reach Done through an event type flagged `is_finish`.

Sample first. Only `item.sample_qty` units exist at the start. The moment that
many units have reached Done, the production batch (`item.total_qty`) is
released into "not started", timestamped by the event that finished the sample.
The release is derived on every replay, never stored: void the finishing entry
and the batch goes back to not existing.

Legacy rows. Events logged before states were removed carry a `state_id`. Each
legacy state gets its own slot inside the step, after the step's arrival slot
and in `sort_order`, so those rows replay exactly as they always did -- a move
from queued to in progress is a move within the step, and keeps the units'
arrival time. The complete state at the last step is Done. Reports read every
slot of a step as "at that stage".

Ordering of steps and legacy states comes from `seq` and `sort_order`. Behaviour
comes from flags on the row. No function in this module branches on a stage,
state or event type *code*; adding a stage or an event type must never require
editing this file.
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
    is_sample: bool = False


@dataclass(frozen=True)
class Position:
    index: int
    item_step_id: uuid.UUID | None
    stage_id: uuid.UUID | None
    seq: int | None
    # Set only on a legacy slot; the arrival slot every new event targets has none.
    state_id: uuid.UUID | None = None
    is_done: bool = False

    @property
    def is_unstarted(self) -> bool:
        return self.index == UNSTARTED_INDEX


@dataclass
class Anomaly:
    """Something the log says that does not add up.

    New writes that over-advance are rejected at the API (see services.events),
    but the log can still contain rows that predate that rule, arrived through
    another path, or conflict only in hindsight after a correction. Replay flags
    those rather than guessing; entries are never edited to make them add up.
    """

    code: str
    event_id: uuid.UUID | None
    item_id: uuid.UUID
    detail: str
    occurred_at: datetime | None = None


@dataclass
class Bucket:
    """Units resting at one place on the item's chain -- not started, one
    stage, or Done -- with the age of the oldest of them."""

    position: Position
    qty: int
    oldest_arrived_at: datetime
    reworked_qty: int
    sample_qty: int = 0
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

    # A correction whose target is not among this item's events is doing
    # nothing: the target either has not synced yet (it will resolve) or landed
    # on a different item (it never will). Either way, say so rather than let
    # the correction sit inert and invisible forever.
    present = {event.id for event in events}
    for target_id, correctors in superseders.items():
        if target_id in present:
            continue
        for corrector in correctors:
            anomalies.append(
                Anomaly(
                    code="orphaned_correction",
                    event_id=corrector.id,
                    item_id=corrector.item_id,
                    detail=(
                        f"correction names event {target_id}, which is not on this "
                        f"item; it has no effect (target unsynced, or on another item)"
                    ),
                    occurred_at=corrector.occurred_at,
                )
            )

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

        self.positions: list[Position] = [
            Position(index=UNSTARTED_INDEX, item_step_id=None, stage_id=None, seq=None)
        ]
        self._index_by_key: dict[tuple[uuid.UUID | None, uuid.UUID | None], int] = {}
        # Every slot of a step points at the step's arrival slot, which is the
        # position reports show for "at this stage".
        self._display_index: dict[int, int] = {UNSTARTED_INDEX: UNSTARTED_INDEX}

        ordered_steps = sorted(steps, key=lambda s: s.seq)
        ordered_states = vocab.ordered_states
        # A legacy row in the queue state is exempt from the station requirement:
        # the queue in front of the paint booths was shared.
        self._legacy_entry_state_id = next(
            (s.id for s in ordered_states if s.is_initial),
            ordered_states[0].id if ordered_states else None,
        )
        last_seq = ordered_steps[-1].seq if ordered_steps else None
        self._stage_by_step: dict[uuid.UUID, Stage] = {}
        self._step_by_id: dict[uuid.UUID, ItemStep] = {}
        done_keys: list[tuple[uuid.UUID, uuid.UUID]] = []
        for step in ordered_steps:
            self._step_by_id[step.id] = step
            stage = vocab.stages.get(step.stage_id)
            if stage is not None:
                self._stage_by_step[step.id] = stage
            arrival = self._add_position(step, state_id=None)
            for state in ordered_states:
                if state.is_complete and (
                    step.seq == last_seq or (stage is not None and stage.is_terminal)
                ):
                    # Completed at the end of the sequence was always "finished".
                    done_keys.append((step.id, state.id))
                    continue
                slot = self._add_position(step, state_id=state.id)
                self._display_index[slot] = arrival

        self.done_index = len(self.positions)
        self.positions.append(
            Position(index=self.done_index, item_step_id=None, stage_id=None, seq=None, is_done=True)
        )
        self._display_index[self.done_index] = self.done_index
        for key in done_keys:
            self._index_by_key[key] = self.done_index

        self.buckets: list[deque[Lot]] = [deque() for _ in self.positions]

        # Sample first: the batch does not exist on the floor until the sample
        # is done. No sample means the batch is there from the start.
        start = item.created_at
        self.sample_qty = max(item.sample_qty or 0, 0)
        self.released_at: datetime | None = None
        if self.sample_qty > 0:
            self.buckets[UNSTARTED_INDEX].append(
                Lot(qty=self.sample_qty, arrived_at=start, is_sample=True)
            )
        else:
            self._release(start)

    def _add_position(self, step: ItemStep, state_id: uuid.UUID | None) -> int:
        index = len(self.positions)
        self.positions.append(
            Position(
                index=index,
                item_step_id=step.id,
                stage_id=step.stage_id,
                seq=step.seq,
                state_id=state_id,
            )
        )
        self._index_by_key[(step.id, state_id)] = index
        self._display_index[index] = index
        return index

    def _release(self, moment: datetime) -> None:
        self.released_at = moment
        if self.item.total_qty > 0:
            self.buckets[UNSTARTED_INDEX].append(Lot(qty=self.item.total_qty, arrived_at=moment))

    # -- replay ---------------------------------------------------------------

    def apply_all(self, events: list[Event]) -> None:
        for event in events:
            self.apply(event)

    def target_index(
        self,
        item_step_id: uuid.UUID | None,
        state_id: uuid.UUID | None = None,
        is_finish: bool = False,
    ) -> int | None:
        """The position an event lands in, or None if it names nothing on
        this item's chain."""
        if is_finish:
            return self.done_index
        if item_step_id is None:
            return None
        return self._index_by_key.get((item_step_id, state_id))

    def source_order(
        self,
        target_index: int,
        is_rework: bool,
        from_item_step_id: uuid.UUID | None = None,
        from_done: bool = False,
    ) -> list[int]:
        """The positions an event landing at `target_index` draws from, in the
        order it drains them.

        Unnamed, that is every position on the right side of the target,
        nearest first: upstream for a move, downstream for rework (rework is
        forward motion -- the units are further along and come back). When the
        entry names where the units came from, only that place is drawn from,
        and only if it lies on the right side; an empty list means the named
        source is not somewhere this event could take units from.
        """
        if is_rework:
            order = list(range(target_index + 1, len(self.positions)))
        else:
            order = list(range(target_index - 1, -1, -1))
        if from_done:
            return [i for i in order if i == self.done_index]
        if from_item_step_id is not None:
            return [i for i in order if self.positions[i].item_step_id == from_item_step_id]
        return order

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
            and event_type.moves_quantity
            and not (event.state_id is not None and event.state_id == self._legacy_entry_state_id)
        ):
            self._flag(
                event,
                "missing_station",
                f"stage {stage.code} records stations but this event has none",
            )

        if not event_type.moves_quantity:
            # Item-level events (a revision bump, say) are history, not movement.
            return

        target_index = self.target_index(
            event.item_step_id, event.state_id, is_finish=event_type.is_finish
        )
        if target_index is None:
            self._flag(
                event,
                "unknown_position",
                "event names a step that is not one of this item's stages",
            )
            return

        if event.qty <= 0:
            return

        sources = self.source_order(
            target_index,
            event_type.is_rework,
            event.from_item_step_id,
            bool(event.from_done),
        )
        named = event.from_item_step_id is not None or bool(event.from_done)
        if named and not sources:
            self._flag(
                event,
                "unknown_position",
                (
                    "event names a source that is not "
                    f"{'downstream' if event_type.is_rework else 'upstream'} of its target "
                    "on this item's sequence"
                ),
            )
            return

        taken, shortfall = self._take(sources, event.qty)
        if shortfall:
            if named:
                where = "at the place it takes them from"
            else:
                where = "downstream" if event_type.is_rework else "upstream"
            self._flag(
                event,
                "over_advance",
                (
                    f"event moves {event.qty} but only {event.qty - shortfall} "
                    f"were available {where}; {shortfall} unaccounted for"
                ),
            )

        destination = self.buckets[target_index]
        target_step = self.positions[target_index].item_step_id
        rework_bump = 1 if event_type.is_rework else 0
        for source_index, lot in taken:
            same_stage = (
                target_step is not None
                and self.positions[source_index].item_step_id == target_step
            )
            destination.append(
                Lot(
                    qty=lot.qty,
                    # A legacy move within one stage (queued -> in progress) is
                    # not an arrival: the units have been at this stage all along.
                    arrived_at=lot.arrived_at if same_stage else event.occurred_at,
                    # Exactly what the event says, never inherited from the source
                    # lot: a stage without stations must not show its units parked
                    # at the previous stage's station.
                    station_id=event.station_id,
                    rework_count=lot.rework_count + rework_bump,
                    is_sample=lot.is_sample,
                )
            )

        if self.released_at is None and self.done_qty >= self.sample_qty:
            self._release(event.occurred_at)

    def _take(self, source_indexes, qty: int) -> tuple[list[tuple[int, Lot]], int]:
        taken: list[tuple[int, Lot]] = []
        remaining = qty
        for index in source_indexes:
            lots = self.buckets[index]
            while lots and remaining > 0:
                lot = lots[0]
                if lot.qty <= remaining:
                    lots.popleft()
                    taken.append((index, lot))
                    remaining -= lot.qty
                else:
                    lot.qty -= remaining
                    taken.append(
                        (
                            index,
                            Lot(
                                qty=remaining,
                                arrived_at=lot.arrived_at,
                                station_id=lot.station_id,
                                rework_count=lot.rework_count,
                                is_sample=lot.is_sample,
                            ),
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
        """Places currently holding units, in sequence order: not started, each
        stage (all of its legacy slots merged), Done."""
        grouped: dict[int, list[Lot]] = {}
        for position in self.positions:
            lots = self.buckets[position.index]
            if not lots:
                continue
            if position.is_unstarted and not include_unstarted:
                continue
            if position.is_done and not include_finished:
                continue
            grouped.setdefault(self._display_index[position.index], []).extend(lots)

        result: list[Bucket] = []
        for index in sorted(grouped):
            lots = grouped[index]
            by_station: dict[uuid.UUID | None, int] = {}
            for lot in lots:
                by_station[lot.station_id] = by_station.get(lot.station_id, 0) + lot.qty
            result.append(
                Bucket(
                    position=self.positions[index],
                    qty=sum(lot.qty for lot in lots),
                    oldest_arrived_at=min(lot.arrived_at for lot in lots),
                    reworked_qty=sum(lot.qty for lot in lots if lot.rework_count > 0),
                    sample_qty=sum(lot.qty for lot in lots if lot.is_sample),
                    by_station=by_station,
                )
            )
        return result

    def qty_at(self, item_step_id: uuid.UUID) -> int:
        """Units at this stage, across every slot of the step."""
        return sum(
            lot.qty
            for position in self.positions
            if position.item_step_id == item_step_id
            for lot in self.buckets[position.index]
        )

    @property
    def done_qty(self) -> int:
        """Every unit in Done, sample included."""
        return sum(lot.qty for lot in self.buckets[self.done_index])

    @property
    def completed_qty(self) -> int:
        """Production units in Done. The sample is not part of the batch."""
        return sum(lot.qty for lot in self.buckets[self.done_index] if not lot.is_sample)

    @property
    def unstarted_qty(self) -> int:
        return sum(lot.qty for lot in self.buckets[UNSTARTED_INDEX])

    @property
    def production_unstarted_qty(self) -> int:
        """Batch units not yet started; the whole batch while the sample runs."""
        if self.released_at is None:
            return self.item.total_qty
        return sum(lot.qty for lot in self.buckets[UNSTARTED_INDEX] if not lot.is_sample)

    @property
    def phase(self) -> str:
        return "production" if self.released_at is not None else "sample"

    def available_upstream(self, target: int, is_rework: bool = False) -> int:
        """How many units an event landing at `target` could legitimately move."""
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
