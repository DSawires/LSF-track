"""The derivation: quantities, splits, corrections, rework, over-advance."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.db import utcnow
from app.models import Event, ReasonCode
from app.services import reports
from app.services.derivation import derive
from app.services.events import EventRejected
from tests.conftest import days_ago, hours_ago


def _bucket(db, item, stage_code, state_code):
    derivation = derive(db, item_ids=[item.id])
    ledger = derivation.ledger_for(item.id)
    for bucket in ledger.occupied(include_finished=True):
        position = bucket.position
        if position.is_unstarted:
            continue
        stage = derivation.vocab.stages[position.stage_id]
        state = next(s for s in derivation.vocab.states if s.id == position.state_id)
        if stage.code == stage_code and state.code == state_code:
            return bucket
    return None


def test_full_batch_walks_the_route(factory):
    route = factory.route("r", ["carpentry", "paint", "packing"])
    item = factory.item("IT-1", 100, route)

    factory.log(item, 10, "queued", 100, at=hours_ago(10), station_code="carpentry_1")
    factory.log(item, 10, "in_progress", 100, at=hours_ago(9), station_code="carpentry_1")
    factory.log(item, 10, "completed", 100, at=hours_ago(8), station_code="carpentry_1")
    factory.log(item, 20, "queued", 100, at=hours_ago(7), station_code="paint_1")

    bucket = _bucket(factory.db, item, "paint", "queued")
    assert bucket.qty == 100
    ledger = derive(factory.db, item_ids=[item.id]).ledger_for(item.id)
    assert ledger.unstarted_qty == 0
    assert ledger.completed_qty == 0


def test_partial_advance_splits_the_batch(factory):
    route = factory.route("r", ["carpentry", "paint"])
    item = factory.item("IT-2", 120, route)

    factory.log(item, 10, "queued", 120, at=hours_ago(20), station_code="carpentry_1")
    factory.log(item, 10, "completed", 120, at=hours_ago(15), station_code="carpentry_1")
    factory.log(item, 20, "queued", 40, at=hours_ago(10), station_code="paint_1")

    assert _bucket(factory.db, item, "carpentry", "completed").qty == 80
    assert _bucket(factory.db, item, "paint", "queued").qty == 40


def test_skipped_intermediate_state_still_resolves(factory):
    """An engineer logs `completed` without ever logging `in_progress`."""
    route = factory.route("r", ["carpentry", "paint"])
    item = factory.item("IT-3", 50, route)

    factory.log(item, 10, "completed", 50, at=hours_ago(5), station_code="carpentry_1")

    assert _bucket(factory.db, item, "carpentry", "completed").qty == 50
    ledger = derive(factory.db, item_ids=[item.id]).ledger_for(item.id)
    assert ledger.unstarted_qty == 0


def test_over_advance_is_rejected_with_the_available_count(factory):
    """CLAUDE.md: advancing more units than exist upstream is a validation
    error. The reason names the number that IS available, so the engineer can
    fix the entry rather than guess."""
    route = factory.route("r", ["carpentry", "paint"])
    item = factory.item("IT-4", 30, route)

    factory.log(item, 10, "completed", 30, at=hours_ago(6), station_code="carpentry_1")
    # Claims 50 moved to paint; only 30 exist.
    with pytest.raises(EventRejected) as excinfo:
        factory.log(item, 20, "queued", 50, at=hours_ago(3), station_code="paint_1")
    assert excinfo.value.field == "qty"
    assert "only 30" in excinfo.value.reason

    # Nothing was stored: the corrected entry goes straight in.
    factory.log(item, 20, "queued", 30, at=hours_ago(3), station_code="paint_1")
    assert _bucket(factory.db, item, "paint", "queued").qty == 30
    derivation = derive(factory.db, item_ids=[item.id])
    assert "over_advance" not in [a.code for a in derivation.anomalies]


def test_preexisting_over_advance_rows_still_flag_on_replay(factory):
    """Rows that predate the write-time guard (or slipped past it in a race)
    are surfaced by the derivation, never silently normalised: the units that
    do exist move, the shortfall is flagged, nothing is invented."""
    route = factory.route("r", ["carpentry", "paint"])
    item = factory.item("IT-4B", 30, route)
    factory.log(item, 10, "completed", 30, at=hours_ago(6), station_code="carpentry_1")

    step = next(s for s in item.steps if s.seq == 20)
    factory.db.add(
        Event(
            id=uuid.uuid4(),
            item_id=item.id,
            item_step_id=step.id,
            station_id=factory.station("paint_1").id,
            event_type_id=factory.event_type("move").id,
            state_id=factory.state("queued").id,
            qty=50,
            occurred_at=hours_ago(3),
            received_at=utcnow(),
            user_id=factory.user.id,
        )
    )
    factory.db.flush()

    derivation = derive(factory.db, item_ids=[item.id])
    assert "over_advance" in [a.code for a in derivation.anomalies]
    assert _bucket(factory.db, item, "paint", "queued").qty == 30


def test_correction_supersedes_and_quantity_returns(factory):
    route = factory.route("r", ["carpentry", "paint"])
    item = factory.item("IT-5", 60, route)

    factory.log(item, 10, "completed", 60, at=hours_ago(8), station_code="carpentry_1")
    wrong = factory.log(item, 20, "queued", 60, at=hours_ago(4), station_code="paint_1")

    # The whole move was wrong; supersede it.
    factory.log(
        item, 20, "queued", 0, at=hours_ago(1),
        event_type="correction", supersedes=wrong.id,
    )

    assert _bucket(factory.db, item, "paint", "queued") is None
    assert _bucket(factory.db, item, "carpentry", "completed").qty == 60


def test_rework_pulls_from_downstream_and_is_excluded_from_completion(factory):
    route = factory.route("r", ["carpentry", "qc", "packing"])
    item = factory.item("IT-6", 20, route)
    reason = factory.db.scalars(sa.select(ReasonCode).limit(1)).first()

    factory.log(item, 10, "completed", 20, at=days_ago(3), station_code="carpentry_1")
    factory.log(item, 20, "completed", 20, at=days_ago(2))
    factory.log(item, 30, "completed", 20, at=days_ago(1.5))
    ledger = derive(factory.db, item_ids=[item.id]).ledger_for(item.id)
    assert ledger.completed_qty == 20

    # QC pulls 5 back to carpentry.
    factory.log(
        item, 10, "queued", 5, at=days_ago(1),
        event_type="rework_return", reason_code_id=reason.id, station_code="carpentry_1",
    )

    ledger = derive(factory.db, item_ids=[item.id]).ledger_for(item.id)
    assert ledger.completed_qty == 15
    bucket = _bucket(factory.db, item, "carpentry", "queued")
    assert bucket.qty == 5
    assert bucket.reworked_qty == 5


def test_replay_is_deterministic_regardless_of_insert_order(factory):
    """Events synced late and out of order derive the same state, and the
    out-of-order arrival is not falsely rejected: the availability check
    simulates the whole occurred_at-ordered log, so a downstream event landing
    before its upstream partner is judged by where the replay seats it."""
    route = factory.route("r", ["carpentry", "paint"])
    item = factory.item("IT-7", 10, route)

    # Logged in reverse: the paint event reaches the server before the carpentry one.
    factory.log(item, 20, "queued", 10, at=hours_ago(2), station_code="paint_1")
    factory.log(item, 10, "completed", 10, at=hours_ago(5), station_code="carpentry_1")

    assert _bucket(factory.db, item, "paint", "queued").qty == 10
    derivation = derive(factory.db, item_ids=[item.id])
    assert not [a for a in derivation.anomalies if a.code == "over_advance"]


def test_rejected_entry_retries_in_after_a_correction_frees_the_units(factory):
    """The recovery path the client's Retry button exists for: an entry the log
    cannot support is rejected, the conflicting entry is voided by a
    correction, and the retry (same client UUID) then lands."""
    route = factory.route("r", ["carpentry", "paint"])
    item = factory.item("IT-7B", 10, route)

    wrong = factory.log(item, 20, "queued", 10, at=hours_ago(6), station_code="paint_1")
    fix_id = uuid.uuid4()
    # All 10 units are already sitting at paint; the log cannot also support
    # completing 10 at carpentry *after* that move.
    with pytest.raises(EventRejected):
        factory.log(
            item, 10, "completed", 10, at=hours_ago(3),
            station_code="carpentry_1", event_id=fix_id,
        )
    factory.log(
        item, 20, "queued", 0, at=hours_ago(1),
        event_type="correction", supersedes=wrong.id,
    )
    factory.log(
        item, 10, "completed", 10, at=hours_ago(3),
        station_code="carpentry_1", event_id=fix_id,
    )

    assert _bucket(factory.db, item, "carpentry", "completed").qty == 10
    assert _bucket(factory.db, item, "paint", "queued") is None


def test_competing_corrections_keep_only_the_last(factory):
    route = factory.route("r", ["carpentry", "paint"])
    item = factory.item("IT-8", 40, route)

    factory.log(item, 10, "completed", 40, at=hours_ago(9), station_code="carpentry_1")
    wrong = factory.log(item, 20, "queued", 40, at=hours_ago(8), station_code="paint_1")

    # Two devices void the same event. A correction only voids; the replacement
    # movement is logged separately as a normal move.
    factory.log(
        item, 20, "queued", 0, at=hours_ago(4),
        event_type="correction", supersedes=wrong.id,
        event_id=uuid.uuid4(),
    )
    factory.log(
        item, 20, "queued", 0, at=hours_ago(2),
        event_type="correction", supersedes=wrong.id,
        event_id=uuid.uuid4(),
    )
    factory.log(item, 20, "queued", 25, at=hours_ago(1), station_code="paint_1")

    derivation = derive(factory.db, item_ids=[item.id])
    assert "competing_correction" in [a.code for a in derivation.anomalies]
    assert _bucket(factory.db, item, "paint", "queued").qty == 25
    assert _bucket(factory.db, item, "carpentry", "completed").qty == 15


def test_orphaned_correction_is_flagged_not_silent(factory):
    """A correction whose target sits on a DIFFERENT item does nothing to this
    item's derivation and must surface as an anomaly instead of sitting inert.
    The API rejects this shape today, so the row is inserted directly -- it
    models data that predates the guard or arrived outside the API."""
    route = factory.route("r", ["carpentry", "paint"])
    item = factory.item("IT-8B", 10, route)
    other = factory.item("IT-8C", 5, route)
    factory.log(item, 10, "completed", 10, at=hours_ago(4), station_code="carpentry_1")
    target = factory.log(other, 10, "queued", 5, at=hours_ago(3), station_code="carpentry_1")

    correction_type = factory.event_type("correction")
    factory.db.add(
        Event(
            id=uuid.uuid4(),
            item_id=item.id,
            event_type_id=correction_type.id,
            qty=0,
            occurred_at=hours_ago(1),
            received_at=utcnow(),
            user_id=factory.user.id,
            supersedes_event_id=target.id,  # exists, but on the other item
        )
    )
    factory.db.flush()

    derivation = derive(factory.db, item_ids=[item.id])
    assert "orphaned_correction" in [a.code for a in derivation.anomalies]
    # The correction excluded nothing: the completed units are untouched.
    assert _bucket(factory.db, item, "carpentry", "completed").qty == 10
    # And the other item's event, which it wrongly names, is untouched too.
    assert _bucket(factory.db, other, "carpentry", "queued").qty == 5


def test_aging_uses_oldest_lot_fifo(factory):
    """A partial advance must not reset the age of what stayed behind."""
    route = factory.route("r", ["carpentry", "paint"])
    item = factory.item("IT-9", 100, route)

    factory.log(item, 10, "queued", 100, at=days_ago(10), station_code="carpentry_1")
    # 60 moved on three days ago; 40 still sitting since day 10.
    factory.log(item, 10, "completed", 60, at=days_ago(3), station_code="carpentry_1")

    bucket = _bucket(factory.db, item, "carpentry", "queued")
    assert bucket.qty == 40
    report = reports.aging_report(factory.db)
    row = next(
        r for r in report["rows"]
        if r["item"]["code"] == "IT-9" and r["stage"] and r["stage"]["code"] == "carpentry"
        and r["state"]["code"] == "queued"
    )
    assert 9.9 < row["days_in_state"] < 10.1


def test_missing_station_is_flagged_only_past_the_queue(factory):
    """The queue in front of a stage's stations is shared, so queued events
    don't need a station; anything past the queue does."""
    route = factory.route("r", ["carpentry"])
    item = factory.item("IT-10", 5, route)
    factory.log(item, 10, "queued", 5, at=hours_ago(2))  # no station: fine
    derivation = derive(factory.db, item_ids=[item.id])
    assert "missing_station" not in [a.code for a in derivation.anomalies]

    factory.log(item, 10, "in_progress", 5, at=hours_ago(1))  # no station: flagged
    derivation = derive(factory.db, item_ids=[item.id])
    assert "missing_station" in [a.code for a in derivation.anomalies]
