"""Derivation of current state from the event log.

Nothing here reads a stored status column, because none exists. Every number the
application reports is produced by replaying `events` through this module.

An item's units rest in places: "not started", any stage in the factory, or
Done. There is no sequence between stages and no route an item must follow. An
event moves `qty` units from the place it names as its source into the place it
names as its target, and that is all it does:

    not started | stage | Done   --qty-->   stage | Done

A unit is at a stage or it is not; there is no queued / in progress / completed
within a stage. Each place holds FIFO lots of units. Units reach Done through
an event type flagged `is_finish`. A move within one stage (to another station)
keeps the lot's arrival time: the units have been at that stage all along.

Sample first. Only `item.sample_qty` units exist at the start. The moment that
many units have reached Done, the production batch (`item.total_qty`) is
released into "not started", timestamped by the event that finished the sample.
The release is derived on every replay, never stored: void the finishing entry
and the batch goes back to not existing.

Linked entries. Units can also leave an item for another one, or arrive
from one: a split into parts, an assembly of parts, or units re-filed into
another phase. Such a row has `to_link` as its destination (the units leave
this item's places altogether) or `from_link` as its source (they appear at
the place the row names, which may be "not started"). Each row is replayed on
its own item like any other; the group they form is checked when it is
written (services.events). A sample sent into parts is still the sample when
it comes back assembled: the first units to return fill the sample units that
left, so finishing them still releases the batch.

A batch split off into another phase before its sample was done has no
sample of its own (`sample_item_id`): it is released when that other item's
sample is, and the replay is told when that was (`release_at`).

Legacy rows. Items used to carry an ordered sequence of steps (`item_steps`),
and an entry named a step and no source: it took its units from the nearest
step before it (or, for rework, after it), so a skipped log resolved itself.
Those rows are still in the log and replay exactly as they did. The item's old
steps become positions in their old order, placed before Done; every other
stage gets a position after it. A row logged before states were removed also
carries a `state_id`, and each legacy state gets its own slot inside its step.
Reports read every slot of a stage -- legacy steps included -- as "at that
stage", and a new entry naming that stage draws from all of them.

Behaviour comes from flags on the row. No function in this module branches on
a stage, state or event type *code*; adding a stage or an event type must never
require editing this file.
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
    stage_id: uuid.UUID | None
    # Set only on a legacy slot: the step it belonged to, its place in the old
    # sequence, and (for rows that predate the removal of states) its state.
    item_step_id: uuid.UUID | None = None
    seq: int | None = None
    state_id: uuid.UUID | None = None
    is_done: bool = False

    @property
    def is_unstarted(self) -> bool:
        return self.index == UNSTARTED_INDEX


@dataclass(frozen=True)
class Move:
    """What one event does: which place it fills, and which it drains, in order.
    `target` is -1 for a row whose units leave the item."""

    target: int
    sources: list[int]
    # The entry said where its units came from, so a shortfall is a shortfall
    # at that place and not "somewhere before it".
    named: bool
    # Free movement between stages, as opposed to a sequence-era row.
    free: bool
    # A linked row: units arriving from another item (no source here), or
    # leaving for one (no target here).
    inflow: bool = False
    outflow: bool = False


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
    # On an over-advance: how many of the event's units were not there to
    # move. The write path words its refusal from this, for the engineer.
    shortfall: int | None = None


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
    """Replays one item's events into places holding FIFO lots."""

    def __init__(
        self,
        item: Item,
        steps: list[ItemStep],
        vocab: Vocabulary,
        parts_link: bool = False,
        release_at: datetime | None = None,
    ) -> None:
        """`parts_link`: this item has parts or is one, so its linked rows are
        splits and assemblies rather than moves between phases. `release_at`:
        for a batch released by another item's sample, when that happened
        (None while it has not)."""
        self.item = item
        self.parts_link = parts_link
        # Units out in parts and not back yet, and how many of them are sample.
        self.in_parts_qty = 0
        self._sample_in_parts = 0
        # Units re-filed into, or out of, this batch from another phase.
        self.transferred_in = 0
        self.transferred_out = 0
        self.vocab = vocab
        self.anomalies: list[Anomaly] = []
        self.applied_count = 0
        self.last_event_at: datetime | None = None

        self.positions: list[Position] = [Position(index=UNSTARTED_INDEX, stage_id=None)]
        # Legacy rows find their slot by (step, state).
        self._index_by_key: dict[tuple[uuid.UUID | None, uuid.UUID | None], int] = {}
        # Every position that counts as "at this stage", in the order a new
        # entry drains them; the first is where a new entry lands.
        self._stage_slots: dict[uuid.UUID, list[int]] = {}

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
        done_keys: list[tuple[uuid.UUID, uuid.UUID]] = []
        for step in ordered_steps:
            stage = vocab.stages.get(step.stage_id)
            if stage is not None:
                self._stage_by_step[step.id] = stage
            self._add_legacy_slot(step, state_id=None)
            for state in ordered_states:
                if state.is_complete and (
                    step.seq == last_seq or (stage is not None and stage.is_terminal)
                ):
                    # Completed at the end of the sequence was always "finished".
                    done_keys.append((step.id, state.id))
                    continue
                self._add_legacy_slot(step, state_id=state.id)

        # Done sits after the legacy steps, which is what lets a sequence-era
        # finish row pull from "everything before it" exactly as it always did.
        self.done_index = len(self.positions)
        self.positions.append(Position(index=self.done_index, stage_id=None, is_done=True))
        for key in done_keys:
            self._index_by_key[key] = self.done_index

        # Every other stage in the factory: an item can be logged at any of
        # them. In a fixed order, so a replay is deterministic.
        for stage in sorted(vocab.stages.values(), key=lambda s: (s.sort_order, s.code)):
            if stage.id not in self._stage_slots:
                index = len(self.positions)
                self.positions.append(Position(index=index, stage_id=stage.id))
                self._stage_slots[stage.id] = [index]

        self.buckets: list[deque[Lot]] = [deque() for _ in self.positions]

        # Sample first: the batch does not exist on the floor until the sample
        # is done. No sample means the batch is there from the start.
        start = item.created_at
        self.sample_qty = max(item.sample_qty or 0, 0)
        self.released_at: datetime | None = None
        self.shares_sample = getattr(item, "sample_item_id", None) is not None
        if self.shares_sample:
            # Another item's sample releases this batch. Released or not, it
            # has no sample of its own to finish.
            self.sample_qty = 0
            if release_at is not None:
                self._release(max(release_at, start))
        elif self.sample_qty > 0:
            self.buckets[UNSTARTED_INDEX].append(
                Lot(qty=self.sample_qty, arrived_at=start, is_sample=True)
            )
        else:
            self._release(start)

    def _add_legacy_slot(self, step: ItemStep, state_id: uuid.UUID | None) -> int:
        index = len(self.positions)
        self.positions.append(
            Position(
                index=index,
                stage_id=step.stage_id,
                item_step_id=step.id,
                seq=step.seq,
                state_id=state_id,
            )
        )
        self._index_by_key[(step.id, state_id)] = index
        self._stage_slots.setdefault(step.stage_id, []).append(index)
        return index

    def _release(self, moment: datetime) -> None:
        self.released_at = moment
        if self.item.total_qty > 0:
            self.buckets[UNSTARTED_INDEX].append(Lot(qty=self.item.total_qty, arrived_at=moment))

    # -- what an event does ---------------------------------------------------

    def resolve(self, event: Event, event_type: EventType) -> tuple[Move | None, str | None]:
        """The move an event describes, or the reason it describes none.

        Shared by the replay and by the write path, so "would this be stored?"
        and "what does it do once stored?" can never disagree.
        """
        free = (
            event.stage_id is not None
            or event.from_stage_id is not None
            or bool(event.from_unstarted)
            or bool(getattr(event, "from_link", False))
            or bool(getattr(event, "to_link", False))
        )
        if free:
            return self._resolve_free(event, event_type)
        return self._resolve_legacy(event, event_type)

    def _resolve_free(self, event: Event, event_type: EventType) -> tuple[Move | None, str | None]:
        from_link = bool(getattr(event, "from_link", False))
        to_link = bool(getattr(event, "to_link", False))
        if from_link and to_link:
            return None, "an entry either takes units out of this item or brings them in, not both"
        if to_link:
            if event.stage_id is not None or event_type.is_finish:
                return None, "units leaving for another item name no stage here"
            target = -1
        elif from_link and event.stage_id is None and not event_type.is_finish:
            # Arriving from another item: they may arrive not started.
            target = UNSTARTED_INDEX
        elif event_type.is_finish:
            if event.stage_id is not None:
                return None, "a finish entry moves units to Done and names no stage"
            target = self.done_index
        else:
            slots = self._stage_slots.get(event.stage_id) if event.stage_id else None
            if not slots:
                return None, "event names no stage, or one that does not exist"
            target = slots[0]

        named = [
            event.from_stage_id is not None,
            bool(event.from_unstarted),
            bool(event.from_done),
            from_link,
        ]
        if sum(named) != 1:
            return None, "event must name exactly one place its units come from"
        if from_link:
            return Move(target=target, sources=[], named=True, free=True, inflow=True), None
        if event.from_unstarted:
            sources = [UNSTARTED_INDEX]
        elif event.from_done:
            sources = [self.done_index]
        else:
            sources = list(self._stage_slots.get(event.from_stage_id, []))
            if not sources:
                return None, "event takes units from a stage that does not exist"
        if target >= 0 and sources == [target] and self.positions[target].is_done:
            return None, "event moves units from Done to Done"
        return Move(target=target, sources=sources, named=True, free=True, outflow=to_link), None

    def _resolve_legacy(
        self, event: Event, event_type: EventType
    ) -> tuple[Move | None, str | None]:
        if event_type.is_finish:
            target = self.done_index
        elif event.item_step_id is None:
            return None, "event names no stage"
        else:
            target = self._index_by_key.get((event.item_step_id, event.state_id))
            if target is None:
                return None, "event names a step that is not one of this item's stages"

        # Sequence-era rule: everything on the right side of the target,
        # nearest first -- before it for a move, after it for rework. Done is
        # the last legacy position; the stages added after it are not part of
        # any sequence, and only a rework row can reach them.
        if event_type.is_rework:
            order = list(range(target + 1, len(self.positions)))
        else:
            order = list(range(target - 1, -1, -1))
        named = event.from_item_step_id is not None or bool(event.from_done)
        if event.from_done:
            order = [i for i in order if i == self.done_index]
        elif event.from_item_step_id is not None:
            order = [i for i in order if self.positions[i].item_step_id == event.from_item_step_id]
        if named and not order:
            side = "after" if event_type.is_rework else "before"
            return None, f"event names a source that is not {side} its target in the item's sequence"
        return Move(target=target, sources=order, named=named, free=False), None

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
            self.vocab.stages.get(event.stage_id)
            if event.stage_id is not None
            else self._stage_by_step.get(event.item_step_id)
            if event.item_step_id is not None
            else None
        )
        if (
            stage is not None
            and stage.requires_station
            and event.station_id is None
            and event_type.moves_quantity
            and not (event.state_id is not None and event.state_id == self._legacy_entry_state_id)
            # Units re-filed into this batch are already at that stage; the
            # row moving them says nothing about a station.
            and not getattr(event, "from_link", False)
        ):
            self._flag(
                event,
                "missing_station",
                f"stage {stage.code} records stations but this event has none",
            )

        if not event_type.moves_quantity:
            # Item-level events (a revision bump, say) are history, not movement.
            return

        move, problem = self.resolve(event, event_type)
        if move is None:
            self._flag(event, "unknown_position", problem or "event describes no move")
            return

        if event.qty <= 0:
            return

        if move.inflow:
            self._arrive(event, move)
            return

        # Units re-filed into another phase are batch units: the sample stays
        # with the item it is the sample of, even where it is the oldest lot at
        # that place -- as it always is in Done.
        transfer_out = move.outflow and not self.parts_link
        taken, shortfall = self._take(move.sources, event.qty, spare_sample=transfer_out)
        if shortfall:
            if move.named:
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
                shortfall=shortfall,
            )

        if move.outflow:
            moved = sum(lot.qty for _index, lot in taken)
            if self.parts_link:
                self.in_parts_qty += moved
                self._sample_in_parts += sum(lot.qty for _i, lot in taken if lot.is_sample)
            else:
                self.transferred_out += moved
            return

        destination = self.buckets[move.target]
        target = self.positions[move.target]
        rework_bump = 1 if event_type.is_rework else 0
        for source_index, lot in taken:
            source = self.positions[source_index]
            # Not an arrival: a move between stations of one stage, or a
            # legacy move between the states of one step. The units have been
            # at this stage all along, so their clock keeps running.
            stayed = (
                move.free and target.stage_id is not None and source.stage_id == target.stage_id
            ) or (target.item_step_id is not None and source.item_step_id == target.item_step_id)
            destination.append(
                Lot(
                    qty=lot.qty,
                    arrived_at=lot.arrived_at if stayed else event.occurred_at,
                    # Exactly what the event says, never inherited from the source
                    # lot: a stage without stations must not show its units parked
                    # at the previous stage's station.
                    station_id=event.station_id,
                    rework_count=lot.rework_count + rework_bump,
                    is_sample=lot.is_sample,
                )
            )

        if self.released_at is None and self.done_qty >= self.sample_qty and not self.shares_sample:
            self._release(event.occurred_at)

    def _arrive(self, event: Event, move: Move) -> None:
        """Units coming in from another item: assembled from parts, split off
        a parent, or re-filed from another phase."""
        qty = event.qty
        lots: list[Lot] = []
        if self.parts_link:
            # Assembled back together. Whatever left as sample returns as the
            # sample: those are the first units back.
            back = min(qty, self.in_parts_qty)
            self.in_parts_qty -= back
            sample = min(qty, self._sample_in_parts)
            self._sample_in_parts -= sample
            if sample:
                lots.append(Lot(qty=sample, arrived_at=event.occurred_at,
                                station_id=event.station_id, is_sample=True))
            qty -= sample
        else:
            self.transferred_in += qty
        if qty:
            lots.append(Lot(qty=qty, arrived_at=event.occurred_at, station_id=event.station_id))
        self.buckets[move.target].extend(lots)
        if self.released_at is None and self.done_qty >= self.sample_qty and not self.shares_sample:
            self._release(event.occurred_at)

    def _take(
        self, source_indexes, qty: int, spare_sample: bool = False
    ) -> tuple[list[tuple[int, Lot]], int]:
        """Oldest lots first. `spare_sample` passes over sample lots, leaving
        them where they are and in their order."""
        taken: list[tuple[int, Lot]] = []
        remaining = qty
        for index in source_indexes:
            lots = self.buckets[index]
            passed: list[Lot] = []
            while lots and remaining > 0:
                lot = lots[0]
                if spare_sample and lot.is_sample:
                    passed.append(lots.popleft())
                elif lot.qty <= remaining:
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
            # Back at the front, oldest first, exactly as they were.
            lots.extendleft(reversed(passed))
            if remaining == 0:
                break
        return taken, remaining

    def _flag(self, event: Event, code: str, detail: str, shortfall: int | None = None) -> None:
        self.anomalies.append(
            Anomaly(
                code=code,
                event_id=event.id,
                item_id=self.item.id,
                detail=detail,
                occurred_at=event.occurred_at,
                shortfall=shortfall,
            )
        )

    # -- reading it back ------------------------------------------------------

    def occupied(self, include_unstarted: bool = True, include_finished: bool = False):
        """Places currently holding units: not started, then each stage (every
        slot of it merged, legacy steps included) in the factory's stage order,
        then Done."""
        grouped: dict[int, list[Lot]] = {}
        for position in self.positions:
            lots = self.buckets[position.index]
            if not lots:
                continue
            if position.is_unstarted and not include_unstarted:
                continue
            if position.is_done and not include_finished:
                continue
            home = (
                self._stage_slots[position.stage_id][0]
                if position.stage_id is not None
                else position.index
            )
            grouped.setdefault(home, []).extend(lots)

        def order(index: int) -> tuple:
            position = self.positions[index]
            if position.is_unstarted:
                return (0, 0, "")
            if position.is_done:
                return (2, 0, "")
            stage = self.vocab.stages.get(position.stage_id)
            return (1, stage.sort_order if stage else 0, stage.code if stage else "")

        result: list[Bucket] = []
        for index in sorted(grouped, key=order):
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

    def qty_in(self, indexes) -> int:
        """Units resting across these positions."""
        return sum(lot.qty for index in indexes for lot in self.buckets[index])

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
    def batch_qty(self) -> int:
        """The production batch as it stands: what it was planned at, plus
        units re-filed into it from another phase, less those re-filed out."""
        return self.item.total_qty + self.transferred_in - self.transferred_out

    @property
    def production_unstarted_qty(self) -> int:
        """Batch units not yet started; the whole batch while the sample runs."""
        if self.released_at is None:
            return self.item.total_qty
        return sum(lot.qty for lot in self.buckets[UNSTARTED_INDEX] if not lot.is_sample)

    @property
    def phase(self) -> str:
        return "production" if self.released_at is not None else "sample"


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
