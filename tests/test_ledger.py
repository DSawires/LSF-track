"""The derivation: quantities, splits, corrections, rework, over-advance,
the sample gate, and legacy state rows."""

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


def _ledger(db, item):
    return derive(db, item_ids=[item.id]).ledger_for(item.id)


def _bucket(db, item, stage_code):
    """Units at a stage, or None if nothing rests there."""
    derivation = derive(db, item_ids=[item.id])
    ledger = derivation.ledger_for(item.id)
    for bucket in ledger.occupied():
        position = bucket.position
        if position.is_unstarted:
            continue
        if derivation.vocab.stages[position.stage_id].code == stage_code:
            return bucket
    return None


def test_full_batch_walks_its_stages(factory):
    item = factory.item("IT-1", 100, ["carpentry", "paint", "packing"])

    factory.log(item, 10, 100, at=hours_ago(10), station_code="carpentry_1")
    factory.log(item, 20, 100, at=hours_ago(7), station_code="paint_1")

    assert _bucket(factory.db, item, "paint").qty == 100
    assert _bucket(factory.db, item, "carpentry") is None
    ledger = _ledger(factory.db, item)
    assert ledger.unstarted_qty == 0
    assert ledger.completed_qty == 0

    factory.log(item, 30, 100, at=hours_ago(4))
    factory.log(item, None, 100, at=hours_ago(1))
    ledger = _ledger(factory.db, item)
    assert ledger.completed_qty == 100
    assert ledger.occupied() == []


def test_partial_advance_splits_the_batch(factory):
    item = factory.item("IT-2", 120, ["carpentry", "paint"])

    factory.log(item, 10, 120, at=hours_ago(20), station_code="carpentry_1")
    factory.log(item, 20, 40, at=hours_ago(10), station_code="paint_1")

    assert _bucket(factory.db, item, "carpentry").qty == 80
    assert _bucket(factory.db, item, "paint").qty == 40


def test_skipped_stage_still_resolves(factory):
    """Units logged at paint that were never logged at carpentry."""
    item = factory.item("IT-3", 50, ["carpentry", "paint"])

    factory.log(item, 20, 50, at=hours_ago(5), station_code="paint_1")

    assert _bucket(factory.db, item, "paint").qty == 50
    assert _ledger(factory.db, item).unstarted_qty == 0


def test_over_advance_is_rejected_with_the_available_count(factory):
    """CLAUDE.md: advancing more units than exist upstream is a validation
    error. The reason names the number that IS available, so the engineer can
    fix the entry rather than guess."""
    item = factory.item("IT-4", 30, ["carpentry", "paint"])

    factory.log(item, 10, 30, at=hours_ago(6), station_code="carpentry_1")
    # Claims 50 moved to paint; only 30 exist.
    with pytest.raises(EventRejected) as excinfo:
        factory.log(item, 20, 50, at=hours_ago(3), station_code="paint_1")
    assert excinfo.value.field == "qty"
    assert "only 30" in excinfo.value.reason

    # Nothing was stored: the corrected entry goes straight in.
    factory.log(item, 20, 30, at=hours_ago(3), station_code="paint_1")
    assert _bucket(factory.db, item, "paint").qty == 30
    derivation = derive(factory.db, item_ids=[item.id])
    assert "over_advance" not in [a.code for a in derivation.anomalies]


def test_preexisting_over_advance_rows_still_flag_on_replay(factory):
    """Rows that predate the write-time guard (or slipped past it in a race)
    are surfaced by the derivation, never silently normalised: the units that
    do exist move, the shortfall is flagged, nothing is invented."""
    item = factory.item("IT-4B", 30, ["carpentry", "paint"])
    factory.log(item, 10, 30, at=hours_ago(6), station_code="carpentry_1")

    step = next(s for s in item.steps if s.seq == 20)
    factory.db.add(
        Event(
            id=uuid.uuid4(),
            item_id=item.id,
            item_step_id=step.id,
            station_id=factory.station("paint_1").id,
            event_type_id=factory.event_type("move").id,
            qty=50,
            occurred_at=hours_ago(3),
            received_at=utcnow(),
            user_id=factory.user.id,
        )
    )
    factory.db.flush()

    derivation = derive(factory.db, item_ids=[item.id])
    assert "over_advance" in [a.code for a in derivation.anomalies]
    assert _bucket(factory.db, item, "paint").qty == 30


def test_correction_supersedes_and_quantity_returns(factory):
    item = factory.item("IT-5", 60, ["carpentry", "paint"])

    factory.log(item, 10, 60, at=hours_ago(8), station_code="carpentry_1")
    wrong = factory.log(item, 20, 60, at=hours_ago(4), station_code="paint_1")

    # The whole move was wrong; supersede it.
    factory.log(item, None, 0, at=hours_ago(1), event_type="correction", supersedes=wrong.id)

    assert _bucket(factory.db, item, "paint") is None
    assert _bucket(factory.db, item, "carpentry").qty == 60


def test_rework_pulls_from_downstream_and_is_excluded_from_completion(factory):
    item = factory.item("IT-6", 20, ["carpentry", "qc", "packing"])
    reason = factory.db.scalars(sa.select(ReasonCode).limit(1)).first()

    factory.log(item, 10, 20, at=days_ago(3), station_code="carpentry_1")
    factory.log(item, 20, 20, at=days_ago(2))
    factory.log(item, 30, 20, at=days_ago(1.8))
    factory.log(item, None, 20, at=days_ago(1.5))
    assert _ledger(factory.db, item).completed_qty == 20

    # QC pulls 5 back to carpentry.
    factory.log(
        item, 10, 5, at=days_ago(1),
        event_type="rework_return", reason_code_id=reason.id, station_code="carpentry_1",
    )

    assert _ledger(factory.db, item).completed_qty == 15
    bucket = _bucket(factory.db, item, "carpentry")
    assert bucket.qty == 5
    assert bucket.reworked_qty == 5


def test_finish_entry_names_no_stage(factory):
    item = factory.item("IT-6B", 5, ["carpentry"])
    step = item.steps[0]
    factory.log(item, 10, 5, at=hours_ago(2), station_code="carpentry_1")
    with pytest.raises(EventRejected, match="Done"):
        from app.schemas import EventCreate
        from app.services.events import record_event

        record_event(
            factory.db,
            EventCreate(
                id=uuid.uuid4(), item_id=item.id, item_step_id=step.id,
                event_type_id=factory.event_type("finish").id, qty=5,
                occurred_at=hours_ago(1),
            ),
            factory.user,
        )


def test_replay_is_deterministic_regardless_of_insert_order(factory):
    """Events synced late and out of order derive the same state, and the
    out-of-order arrival is not falsely rejected: the availability check
    simulates the whole occurred_at-ordered log, so a downstream event landing
    before its upstream partner is judged by where the replay seats it."""
    item = factory.item("IT-7", 10, ["carpentry", "paint"])

    # Logged in reverse: the paint event reaches the server before the carpentry one.
    factory.log(item, 20, 10, at=hours_ago(2), station_code="paint_1")
    factory.log(item, 10, 10, at=hours_ago(5), station_code="carpentry_1")

    assert _bucket(factory.db, item, "paint").qty == 10
    derivation = derive(factory.db, item_ids=[item.id])
    assert not [a for a in derivation.anomalies if a.code == "over_advance"]


def test_rejected_entry_retries_in_after_a_correction_frees_the_units(factory):
    """The recovery path the client's Retry button exists for: an entry the log
    cannot support is rejected, the conflicting entry is voided by a
    correction, and the retry (same client UUID) then lands."""
    item = factory.item("IT-7B", 10, ["carpentry", "paint", "qc"])

    wrong = factory.log(item, 30, 10, at=hours_ago(6))
    fix_id = uuid.uuid4()
    # All 10 units are already at QC; the log cannot also support 10 arriving
    # at paint *after* that move.
    with pytest.raises(EventRejected):
        factory.log(item, 20, 10, at=hours_ago(3), station_code="paint_1", event_id=fix_id)
    factory.log(item, None, 0, at=hours_ago(1), event_type="correction", supersedes=wrong.id)
    factory.log(item, 20, 10, at=hours_ago(3), station_code="paint_1", event_id=fix_id)

    assert _bucket(factory.db, item, "paint").qty == 10
    assert _bucket(factory.db, item, "qc") is None


def test_competing_corrections_keep_only_the_last(factory):
    item = factory.item("IT-8", 40, ["carpentry", "paint"])

    factory.log(item, 10, 40, at=hours_ago(9), station_code="carpentry_1")
    wrong = factory.log(item, 20, 40, at=hours_ago(8), station_code="paint_1")

    # Two devices void the same event. A correction only voids; the replacement
    # movement is logged separately as a normal move.
    for at in (hours_ago(4), hours_ago(2)):
        factory.log(
            item, None, 0, at=at, event_type="correction", supersedes=wrong.id,
            event_id=uuid.uuid4(),
        )
    factory.log(item, 20, 25, at=hours_ago(1), station_code="paint_1")

    derivation = derive(factory.db, item_ids=[item.id])
    assert "competing_correction" in [a.code for a in derivation.anomalies]
    assert _bucket(factory.db, item, "paint").qty == 25
    assert _bucket(factory.db, item, "carpentry").qty == 15


def test_orphaned_correction_is_flagged_not_silent(factory):
    """A correction whose target sits on a DIFFERENT item does nothing to this
    item's derivation and must surface as an anomaly instead of sitting inert.
    The API rejects this shape today, so the row is inserted directly -- it
    models data that predates the guard or arrived outside the API."""
    item = factory.item("IT-8B", 10, ["carpentry", "paint"])
    other = factory.item("IT-8C", 5, ["carpentry", "paint"])
    factory.log(item, 10, 10, at=hours_ago(4), station_code="carpentry_1")
    target = factory.log(other, 10, 5, at=hours_ago(3), station_code="carpentry_1")

    factory.db.add(
        Event(
            id=uuid.uuid4(),
            item_id=item.id,
            event_type_id=factory.event_type("correction").id,
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
    # The correction excluded nothing on either item.
    assert _bucket(factory.db, item, "carpentry").qty == 10
    assert _bucket(factory.db, other, "carpentry").qty == 5


def test_aging_uses_oldest_lot_fifo(factory):
    """A partial advance must not reset the age of what stayed behind."""
    item = factory.item("IT-9", 100, ["carpentry", "paint"])

    factory.log(item, 10, 100, at=days_ago(10), station_code="carpentry_1")
    # 60 moved on three days ago; 40 still sitting since day 10.
    factory.log(item, 20, 60, at=days_ago(3), station_code="paint_1")

    assert _bucket(factory.db, item, "carpentry").qty == 40
    report = reports.aging_report(factory.db)
    row = next(
        r for r in report["rows"]
        if r["item"]["code"] == "IT-9" and r["stage"] and r["stage"]["code"] == "carpentry"
    )
    assert 9.9 < row["days_in_state"] < 10.1


def test_overdue_is_judged_against_the_stage_threshold(factory):
    """Sitting too long is per stage, not one global number: 5 days at a
    3-day stage is overdue, 5 days at a no-threshold stage is not."""
    hot = factory.stage("paint")
    hot.max_days_in_state = 3
    factory.add_stage("resting", sort=35, max_days_in_state=None)
    item = factory.item("AGE-1", 20, ["paint", "resting"])

    factory.log(item, 10, 20, at=days_ago(9), station_code="paint_1")
    factory.log(item, 20, 10, at=days_ago(5))

    report = reports.aging_report(factory.db)
    by_stage = {r["stage"]["code"]: r for r in report["rows"] if r["stage"]}
    assert by_stage["paint"]["overdue"] is True       # 9d >= 3d
    assert by_stage["resting"]["overdue"] is False    # no threshold
    # The item-card payload carries the same judgement.
    state = reports.item_state(factory.db, [item.id])[str(item.id)]
    flags = {p["stage_code"]: p["overdue"] for p in state["positions"] if p["stage_code"]}
    assert flags["paint"] is True
    assert flags["resting"] is False


def test_clock_anomalies_flag_ahead_devices_and_late_syncs(factory):
    """CLAUDE.md: received_at exists to detect wrong device clocks and late
    syncs -- flagged, not silently trusted."""
    from datetime import timedelta

    item = factory.item("CLK-1", 5, ["carpentry", "paint"])

    # Synced 20 days after it happened: past the 14-day late-sync threshold.
    factory.log(item, 10, 5, at=days_ago(20), station_code="carpentry_1")
    # Device clock 3h ahead: within the 7-day hard-reject bound, past the
    # 1h flag threshold.
    factory.log(item, 20, 5, at=utcnow() + timedelta(hours=3), station_code="paint_1")

    codes = [a.code for a in derive(factory.db, item_ids=[item.id]).anomalies]
    assert "device_clock_ahead" in codes
    assert "late_sync" in codes


def test_far_future_occurred_at_is_rejected(factory):
    from datetime import timedelta

    item = factory.item("CLK-2", 5, ["carpentry"])
    with pytest.raises(EventRejected, match="future"):
        factory.log(item, 10, 5, at=utcnow() + timedelta(days=30), station_code="carpentry_1")


def test_missing_station_is_flagged_at_a_station_stage(factory):
    item = factory.item("IT-10", 5, ["qc", "carpentry"])
    factory.log(item, 10, 5, at=hours_ago(2))  # QC records no stations: fine
    assert "missing_station" not in [
        a.code for a in derive(factory.db, item_ids=[item.id]).anomalies
    ]

    factory.log(item, 20, 5, at=hours_ago(1))  # carpentry does: flagged
    assert "missing_station" in [
        a.code for a in derive(factory.db, item_ids=[item.id]).anomalies
    ]


# -- the sample gate ----------------------------------------------------------


def test_only_the_sample_exists_until_it_is_done(factory):
    item = factory.item("SMP-1", 120, ["carpentry", "paint"], sample_qty=1)

    ledger = _ledger(factory.db, item)
    assert ledger.phase == "sample"
    assert ledger.unstarted_qty == 1

    factory.log(item, 10, 1, at=hours_ago(10), station_code="carpentry_1")
    # The batch is not on the floor yet: nothing upstream to move.
    with pytest.raises(EventRejected, match="only 0"):
        factory.log(item, 10, 5, at=hours_ago(9), station_code="carpentry_1")

    factory.log(item, 20, 1, at=hours_ago(8), station_code="paint_1")
    bucket = _bucket(factory.db, item, "paint")
    assert (bucket.qty, bucket.sample_qty) == (1, 1)
    assert _ledger(factory.db, item).phase == "sample"


def test_finishing_the_sample_releases_the_batch_down_the_same_path(factory):
    item = factory.item("SMP-2", 120, ["carpentry", "paint"], sample_qty=1)
    factory.log(item, 10, 1, at=hours_ago(10), station_code="carpentry_1")
    factory.log(item, 20, 1, at=hours_ago(8), station_code="paint_1")
    finished = hours_ago(6)
    factory.log(item, None, 1, at=finished)

    ledger = _ledger(factory.db, item)
    assert ledger.phase == "production"
    assert ledger.released_at == finished
    assert ledger.unstarted_qty == 120
    # The sample is not part of the batch count.
    assert ledger.completed_qty == 0
    assert ledger.done_qty == 1

    factory.log(item, 10, 120, at=hours_ago(4), station_code="carpentry_1")
    factory.log(item, 20, 50, at=hours_ago(2), station_code="paint_1")
    assert _bucket(factory.db, item, "carpentry").qty == 70
    assert _bucket(factory.db, item, "paint").sample_qty == 0

    state = reports.item_state(factory.db, [item.id])[str(item.id)]
    assert state["phase"] == "production"
    assert state["sample_done_at"] is not None
    assert state["production_unstarted_qty"] == 0


def test_batch_age_counts_from_the_release_not_the_import(factory):
    item = factory.item("SMP-3", 10, ["carpentry"], sample_qty=1)
    item.created_at = days_ago(20)
    factory.log(item, 10, 1, at=days_ago(19), station_code="carpentry_1")
    factory.log(item, None, 1, at=days_ago(2))

    rows = [
        r for r in reports.aging_report(factory.db)["rows"] if r["item"]["code"] == "SMP-3"
    ]
    assert len(rows) == 1 and rows[0]["is_unstarted"]
    assert 1.9 < rows[0]["days_in_state"] < 2.1


def test_voiding_the_sample_finish_takes_the_batch_back_off_the_floor(factory):
    item = factory.item("SMP-4", 10, ["carpentry"], sample_qty=1)
    factory.log(item, 10, 1, at=hours_ago(10), station_code="carpentry_1")
    finish = factory.log(item, None, 1, at=hours_ago(8))
    factory.log(item, None, 0, at=hours_ago(1), event_type="correction", supersedes=finish.id)

    ledger = _ledger(factory.db, item)
    assert ledger.phase == "sample"
    assert ledger.unstarted_qty == 0
    assert _bucket(factory.db, item, "carpentry").sample_qty == 1


def test_batch_entry_before_the_sample_finished_is_rejected(factory):
    """Out-of-order sync cannot smuggle production ahead of its sample: the
    replay is by occurred_at, and before the finish there is no batch."""
    item = factory.item("SMP-5", 10, ["carpentry"], sample_qty=1)
    factory.log(item, 10, 1, at=hours_ago(10), station_code="carpentry_1")
    factory.log(item, None, 1, at=hours_ago(5))
    with pytest.raises(EventRejected):
        factory.log(item, 10, 10, at=hours_ago(7), station_code="carpentry_1")
    factory.log(item, 10, 10, at=hours_ago(4), station_code="carpentry_1")


# -- rows logged before states were removed -----------------------------------


def test_legacy_state_rows_replay_as_stage_positions(factory):
    """The live log holds queued / in progress / completed rows. They must
    derive the way they always did, read back as "at that stage", with the
    arrival time of the first row at the stage -- a move within one stage is
    not an arrival."""
    item = factory.item("OLD-1", 30, ["carpentry", "paint"])
    factory.log(item, 10, 30, at=days_ago(6), legacy_state="queued")
    factory.log(item, 10, 30, at=days_ago(5), station_code="carpentry_1", legacy_state="in_progress")
    factory.log(item, 10, 30, at=days_ago(4), station_code="carpentry_1", legacy_state="completed")
    factory.log(item, 20, 10, at=days_ago(3), legacy_state="queued")

    carpentry = _bucket(factory.db, item, "carpentry")
    assert carpentry.qty == 20
    assert (utcnow() - carpentry.oldest_arrived_at).days == 6
    assert _bucket(factory.db, item, "paint").qty == 10

    # New stateless entries carry on from where the legacy rows left off.
    factory.log(item, 20, 20, at=days_ago(2), station_code="paint_1")
    assert _bucket(factory.db, item, "carpentry") is None
    assert _bucket(factory.db, item, "paint").qty == 30

    # Completed at the last stage was always "finished".
    factory.log(item, 20, 30, at=days_ago(1), station_code="paint_1", legacy_state="completed")
    assert _ledger(factory.db, item).completed_qty == 30
    anomalies = derive(factory.db, item_ids=[item.id]).anomalies
    assert not [a for a in anomalies if a.code in ("over_advance", "unknown_position")]
